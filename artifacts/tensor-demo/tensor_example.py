import torch


def tensor_example():
    axis = torch.linspace(-3, 3, 48)
    x, y, z = torch.meshgrid(axis, axis, axis, indexing='ij')
    radius = torch.sqrt(x*x + y*y)
    # [spin=0.196, tilt=0.739]
    torus = torch.exp(-5 * ((radius - 1.7)**2 + z*z))
    return torus