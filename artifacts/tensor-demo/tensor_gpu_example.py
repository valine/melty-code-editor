import torch


def tensor_gpu_example():
    axis = torch.linspace(-3, 3, 48, device="cuda")
    x, y, z = torch.meshgrid(axis, axis, axis, indexing="ij")
    radius = torch.sqrt(x*x + y*y)
    torus = torch.exp(-5 * ((radius - 1.7)**2 + z*z))
    waves = torch.sin(axis)
