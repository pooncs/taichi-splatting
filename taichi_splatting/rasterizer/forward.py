
from functools import cache
import taichi as ti
from taichi_splatting.data_types import RasterConfig
from taichi_splatting.rasterizer import tiling
from taichi_splatting.taichi_lib.f32 import conic_pdf, Gaussian2D



@cache
def forward_kernel(config: RasterConfig, feature_size: int):

  feature_vec = ti.types.vector(feature_size, dtype=ti.f32)
  tile_size = config.tile_size

  tile_area = tile_size * tile_size
  thread_pixels = config.pixel_stride[0] * config.pixel_stride[1]


  block_area = tile_area // thread_pixels

  thread_features = ti.types.matrix(thread_pixels, feature_size, dtype=ti.f32)
  thread_alphas = ti.types.vector(thread_pixels, dtype=ti.f32)

  @ti.kernel
  def _forward_kernel(
      points: ti.types.ndarray(Gaussian2D.vec, ndim=1),  # (M, 6)
      point_features: ti.types.ndarray(feature_vec, ndim=1),  # (M, F)
      
      # (TH, TW, 2) the start/end (0..K] index of ranges in the overlap_to_point array
      tile_overlap_ranges: ti.types.ndarray(ti.math.ivec2, ndim=1),
      # (K) ranges of points mapping to indexes into points list
      overlap_to_point: ti.types.ndarray(ti.i32, ndim=1),
      
      # outputs
      image_feature: ti.types.ndarray(feature_vec, ndim=2),  # (H, W, F)
      # needed for backward
      image_alpha: ti.types.ndarray(ti.f32, ndim=2),       # H, W
      image_last_valid: ti.types.ndarray(ti.i32, ndim=2),  # H, W
  ):

    camera_height, camera_width = image_feature.shape

    # round up
    tiles_wide = (camera_width + tile_size - 1) // tile_size 
    tiles_high = (camera_height + tile_size - 1) // tile_size

    # put each tile_size * tile_size tile in the same CUDA thread group (block)
    # tile_id is the index of the tile in the (tiles_wide x tiles_high) grid
    # tile_idx is the index of the pixel in the tile
    # pixels are blocked first by tile_id, then by tile_idx into (8x4) warps
    

    ti.loop_config(block_dim=(block_area))
    for tile_id, tile_idx in ti.ndrange(tiles_wide * tiles_high, block_area):

      pixel = tiling.tile_transform(tile_id, tile_idx, 
                        tile_size, config.pixel_stride, tiles_wide)

      # The initial value of accumulated alpha (initial value of accumulated multiplication)
      T_i =  thread_alphas(1.0)
      accum_feature = thread_features(0.)

      # open the shared memory
      tile_point = ti.simt.block.SharedArray((block_area, ), dtype=Gaussian2D.vec)
      tile_feature = ti.simt.block.SharedArray((block_area, ), dtype=feature_vec)

      start_offset, end_offset = tile_overlap_ranges[tile_id]
      tile_point_count = end_offset - start_offset

      num_point_groups = (tile_point_count + ti.static(block_area - 1)) // block_area
      pixel_saturated = False
      last_point_idx = start_offset

      # Loop through the range in groups of block_area
      for point_group_id in range(num_point_groups):

        ti.simt.block.sync()

        # The offset of the first point in the group
        group_start_offset = start_offset + point_group_id * block_area

        # each thread in a block loads one point into shared memory
        # then all threads in the block process those points sequentially
        load_index = group_start_offset + tile_idx

        if load_index < end_offset:
          point_idx = overlap_to_point[load_index]

  
          tile_point[tile_idx] = points[point_idx]
          tile_feature[tile_idx] = point_features[point_idx]


        ti.simt.block.sync()

        max_point_group_offset: ti.i32 = ti.min(
            block_area, tile_point_count - point_group_id * block_area)

        # in parallel across a block, render all points in the group
        for in_group_idx in range(max_point_group_offset):
          if pixel_saturated:
            break

          uv, uv_conic, point_alpha = Gaussian2D.unpack(tile_point[in_group_idx])
          pixel_saturated = True

          
          for i in ti.static(range(thread_pixels)): 
            pixel_offset = ti.math.ivec2(ti.static(i % config.pixel_stride[0]),
              ti.static(i / config.pixel_stride[0]))
            
            gaussian_alpha = conic_pdf(ti.cast(pixel_offset + pixel, ti.f32) + 0.5, uv, uv_conic)
            alpha = point_alpha * gaussian_alpha

              
            # from paper: we skip any blending updates with 𝛼 < 𝜖 (we choose 𝜖 as 1
            # 255 ) and also clamp 𝛼 with 0.99 from above.
            if alpha < ti.static(config.alpha_threshold):
              alpha = 0.

            alpha = ti.min(alpha, ti.static(config.clamp_max_alpha))
            # from paper: before a Gaussian is included in the forward rasterization
            # pass, we compute the accumulated opacity if we were to include it
            # and stop front-to-back blending before it can exceed 0.9999.
            next_T_i = T_i[i] * (1 - alpha)

            if next_T_i > ti.static(1 - config.saturate_threshold):
              pixel_saturated = False

              last_point_idx = group_start_offset + in_group_idx + 1

              # weight = alpha * T_i
              accum_feature[i, :] += tile_feature[in_group_idx] * alpha * T_i[i]
              T_i[i] = next_T_i


        # end of point group loop
      # end of point group id loop

      for i in ti.static(range(thread_pixels)): 
        pos = pixel + ti.math.ivec2(ti.static(i % config.pixel_stride[0]),
            ti.static(i / config.pixel_stride[0]))
        
        if pos.x < camera_width and pos.y < camera_height:
          image_feature[pos.y, pos.x] = accum_feature[i, :]

          # No need to accumulate a normalisation factor as it is exactly 1 - T_i
          image_alpha[pos.y, pos.x] = 1. - T_i[i]    
          image_last_valid[pos.y, pos.x] = last_point_idx

    # end of pixel loop

  return _forward_kernel




