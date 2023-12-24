
from numbers import Integral
from typing import Tuple
import torch
import math
from taichi_splatting.data_types import Gaussians2D

from taichi_splatting.tile_mapper import map_to_tiles
from taichi_splatting.rasterizer import rasterize, Config


def project_gaussians2d(points: Gaussians2D) -> torch.Tensor:
    scale = torch.exp(points.log_scaling)
    alpha = torch.sigmoid(points.alpha_logit)

    v1 = points.rotation / torch.norm(points.rotation, dim=1, keepdim=True)
    v2 = torch.stack([-v1[..., 1], v1[..., 0]], dim=-1)

    basis = torch.stack([v1, v2], dim=2) * scale.unsqueeze(-1)
    inv_cov = torch.inverse(torch.bmm(basis.transpose(1, 2), basis))

    conic = torch.stack([inv_cov[..., 0, 0], inv_cov[..., 0, 1], inv_cov[..., 1, 1]], dim=-1)
    return torch.cat([points.position, conic, alpha.unsqueeze(1)], dim=-1)  
    

def pad_to_tile(image_size: Tuple[Integral, Integral], tile_size: int):
  def pad(x):
    return int(math.ceil(x / tile_size) * tile_size)
 
  return tuple(pad(x) for x in image_size)


def check_finite(tensor_dict):
  for k, v in tensor_dict.items():
    n = (~torch.isfinite(v)).sum()
    if n > 0:
      raise ValueError(f'Found {n} non-finite values in {k}')


def render_gaussians(
      gaussians: Gaussians2D,
      image_size: Tuple[Integral, Integral],
      tile_size: int = 16,
      debug=False
    ):
  
  if debug:
    check_finite(gaussians)

  gaussians2d = project_gaussians2d(gaussians)
  padded = pad_to_tile(image_size, tile_size)

  overlap_to_point, ranges = map_to_tiles(gaussians2d, gaussians.depth, 
    image_size=padded, tile_size=tile_size)
  
  raster_config = Config(tile_size=tile_size)

  image = rasterize(gaussians=gaussians2d, features=gaussians.feature, 
    tile_overlap_ranges=ranges, overlap_to_point=overlap_to_point,
    image_size=padded, config=raster_config)
  
  w, h = image_size
  return image[:h, :w]

