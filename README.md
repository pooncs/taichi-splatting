# Taichi Splatting

Rasterizer for Guassian Splatting using Taichi and PyTorch - embedded in python library. 

This work is largely derived off [Taichi 3D Gaussian Splatting](https://github.com/wanmeihuali/taichi_3d_gaussian_splatting)

Key differences are the rendering algorithm is decomposed into separate operations (projection, shading functions, tile mapping and rasterization) which can be combined in different ways in order to facilitate a more flexible use. Using the Taichi autodiff for a simpler implementation. 

Examples:
  * Projecting features for lifting 2D to 3D,
  * Colours via. spherical harmonics
  * Depth covariance without needing to build it into the renderer and remaining differentiable.
  * Fully differentiable camera parameters (and ability to swap in new camera models)

## Major dependencies

* Taichi >= 1.7.0
* Torch >= 1.8 (probably works with earlier versions, too)

## Installing

* Clone down with `git clone` and install with `pip install ./taichi-spatting`
* `pip install taichi-splatting`


## Conventions

### Transformation matrices

Transformations are notated `T_x_y`, for example `T_camera_world` can be used to transform points in the world to points in the local camera by `points_camera = T_camera_world @ points_world`


## Progress

### Done
* Simple view culling 
* Projection (no gradient yet)
* Tile mapping 
* Rasterizer forward pass
* Spherical harmonics with autograd

### Todo
* Port rasterizer backward pass
* Projection autograd wrapper
* Training code (likely different repository)

### Improvements

* Exposed all internal constants as parameters
* Switched to matrices as inputs instead of quaternions
* Tile mapping tighter culling for tile overlaps (~30% less rendered splats!)
* All configuration parameters exposed (e.g. tile_size, saturation threshold etc.)




