
from functools import cache
import taichi as ti
from taichi.math import ivec2
from taichi_splatting.data_types import RasterConfig
from taichi_splatting.taichi_lib.concurrent import atomic_add_vector, block_reduce_i32, morton_tile_inv, warp_add_vector

from taichi_splatting.taichi_lib.f32 import conic_pdf_with_grad, Gaussian2D


@cache
def backward_kernel(config: RasterConfig,
                    points_requires_grad: bool,
                    features_requires_grad: bool, 
                    feature_size: int):

  feature_vec = ti.types.vector(feature_size, dtype=ti.f32)
  tile_size = config.tile_size
  tile_area = tile_size * tile_size


  @ti.kernel
  def _backward_kernel(
      points: ti.types.ndarray(Gaussian2D.vec, ndim=1),  # (M, 6)
      point_features: ti.types.ndarray(feature_vec, ndim=1),  # (M, F)
      
      # (TH, TW, 2) the start/end (0..K] index of ranges in the overlap_to_point array
      tile_overlap_ranges: ti.types.ndarray(ti.math.ivec2, ndim=1),
      # (K) ranges of points mapping to indexes into points list
      overlap_to_point: ti.types.ndarray(ti.i32, ndim=1),
      
      # saved from forward
      image_feature: ti.types.ndarray(feature_vec, ndim=2),  # (H, W, F)
      image_alpha: ti.types.ndarray(ti.f32, ndim=2),       # H, W
      image_last_valid: ti.types.ndarray(ti.i32, ndim=2),  # H, W

      # input gradients
      grad_image_feature: ti.types.ndarray(feature_vec, ndim=2),  # (H, W, F)

      # output gradients
      grad_points: ti.types.ndarray(Gaussian2D.vec, ndim=1),  # (M, 6)
      grad_features: ti.types.ndarray(feature_vec, ndim=1),  # (M, F)
  ):

    camera_height, camera_width = image_feature.shape

    # round up
    tiles_wide = (camera_width + tile_size - 1) // tile_size 
    tiles_high = (camera_height + tile_size - 1) // tile_size

    # put each tile_size * tile_size tile in the same CUDA thread group (block)
    ti.loop_config(block_dim=(tile_area))
    for tile_u, tile_v, i in ti.ndrange(tiles_wide, tiles_high, tile_area):
      
      # use morton z-ordering for the threads mapping to pixel values
      # helps a little with grouping warps on the same gaussians
      u, v = morton_tile_inv(i)
      
      pixel = ivec2(tile_u, tile_v) * tile_size + ivec2(u, v) 
      tile_id = tile_u + tile_v * tiles_wide
      thread_id = u + v * tile_size


      # open the shared memory
      tile_point_id = ti.simt.block.SharedArray((tile_area, ), dtype=ti.i32)
      tile_point = ti.simt.block.SharedArray((tile_area, ), dtype=Gaussian2D.vec)
      tile_feature = ti.simt.block.SharedArray((tile_area, ), dtype=feature_vec)

      tile_grad_point = ti.simt.block.SharedArray((tile_area, ), dtype=Gaussian2D.vec)
      tile_grad_feature = ti.simt.block.SharedArray((tile_area,), dtype=feature_vec)
      
      last_point_idx = 0
      accumulated_alpha: ti.f32 = 0.0
      grad_pixel_feature = feature_vec(0.0)

      if pixel.y < camera_height and pixel.x < camera_width:
        last_point_idx = image_last_valid[pixel.y, pixel.x]
        accumulated_alpha: ti.f32 = image_alpha[pixel.y, pixel.x]
        grad_pixel_feature = grad_image_feature[pixel.y, pixel.x]

      T_i = 1.0 - accumulated_alpha  
      w_i = feature_vec(0.0)

      #  T_i = \prod_{j=1}^{i-1} (1 - a_j) \\
      #  \frac{dC}{da_i} = c_i T(i) - \frac{1}{1 - a_i} \\
      #  \sum_{j=i+1}^{n} c_j a_j T(j) \\
      #  \text{let } w_i = \sum_{j=i+1}^{n} c_j a_j T(j) \\
      #  w_n = 0 \\
      #  w_{i-1} = w_i + c_i a_i T(i) \\
      #  \frac{dC}{da_i} = c_i T(i) - \frac{1}{1 - a_i} w_i \\

      # fine tune the end offset to the actual number of points renderered
      end_offset = block_reduce_i32(last_point_idx, ti.max, ti.atomic_max, 0)

      start_offset, _ = tile_overlap_ranges[tile_id]
      tile_point_count = end_offset - start_offset

      num_point_groups = (tile_point_count + ti.static(tile_area - 1)) // tile_area

      # Loop through the range in groups of tile_area
      for point_group_id in range(num_point_groups):

        ti.simt.block.sync() 

        # load points and features into block shared memory
        group_offset_base = point_group_id * tile_area

        block_end_idx = end_offset - group_offset_base
        block_start_idx = ti.max(block_end_idx - tile_area, 0)

        load_index = block_end_idx - thread_id - 1
        if load_index >= block_start_idx:
          point_idx = overlap_to_point[load_index]

          tile_point_id[thread_id] = point_idx
          tile_point[thread_id] = points[point_idx]
          tile_feature[thread_id] = point_features[point_idx]

          tile_grad_point[thread_id] = Gaussian2D.vec(0.0)
          tile_grad_feature[thread_id] = feature_vec(0.0)

        ti.simt.block.sync()

        point_group_size = ti.min(
          tile_area, tile_point_count - group_offset_base)
                    
        for in_group_idx in range(point_group_size):
          point_index = end_offset - (group_offset_base + in_group_idx)

          if not ti.simt.warp.any_nonzero(ti.u32(0xffffffff), ti.i32(point_index <= last_point_idx)):
            continue

          uv, uv_conic, point_alpha = Gaussian2D.unpack(tile_point[in_group_idx])

          gaussian_alpha, dp_dmean, dp_dconic = conic_pdf_with_grad(
            ti.cast(pixel, ti.f32) + 0.5, uv, uv_conic)
          
          alpha = point_alpha * gaussian_alpha
          has_grad = (alpha >= ti.static(config.alpha_threshold)) and (point_index <= last_point_idx)      

          grad_point = Gaussian2D.vec(0.0)
          grad_feature = feature_vec(0.0)

          if has_grad:
            alpha = ti.min(alpha, ti.static(config.clamp_max_alpha))
            T_i = T_i / (1. - alpha)

            feature = tile_feature[in_group_idx]

            # \frac{dC}{da_i} = c_i T(i) - \frac{1}{1 - a_i} w_i
            alpha_grad_from_feature = (feature * T_i - w_i / (1. - alpha)
                                      ) * grad_pixel_feature

            # w_{i-1} = w_i + c_i a_i T(i)
            w_i += feature * alpha * T_i
            alpha_grad: ti.f32 = alpha_grad_from_feature.sum()

            grad_point = alpha_grad * Gaussian2D.to_vec(
                point_alpha * dp_dmean, 
                point_alpha * dp_dconic,
                gaussian_alpha)

            grad_feature = alpha * T_i * grad_pixel_feature    

          if ti.simt.warp.any_nonzero(ti.u32(0xffffffff), ti.i32(has_grad)):

            # Accumulating gradients in block shared memory does not appear to be faster
            # on it's own, but combined with warp sums it seems to be fast
            if ti.static(points_requires_grad):
              warp_add_vector(tile_grad_point[in_group_idx], grad_point)
            
            if ti.static(features_requires_grad):
              warp_add_vector(tile_grad_feature[in_group_idx], grad_feature)
        # end of point group loop

        ti.simt.block.sync()

        # finally accumulate gradients in global memory
        if load_index >= block_start_idx:
          point_offset = tile_point_id[thread_id] 
          if ti.static(points_requires_grad):
            atomic_add_vector(grad_points[point_offset], tile_grad_point[thread_id])

          if ti.static(features_requires_grad):
            atomic_add_vector(grad_features[point_offset], tile_grad_feature[thread_id])

      # end of point group id loop
    # end of pixel loop

  return _backward_kernel




