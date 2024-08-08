import pytest
import torch

from vllm.lora.layers import _apply_lora, _apply_lora_packed_nslice

from .utils import DummyLoRAManager

TENSOR_SIZES = [128, 1024, 2048, 4096, 8192, 11008, 11008 // 2, 11008 // 4]
QKV_TENSOR_SIZES = [
    (8192, 1024, 1024),
    (8192 // 8, 1024 // 8, 1024 // 8),
    (4096, 4096, 4096),
    (4096 // 2, 4096 // 2, 4096 // 2),
]
BATCH_SIZES = [8, 32, 256]
RANKS = [8]
DTYPES = [torch.float16]
TOLERANCES = {
    torch.float16: (5e-3, 5e-3),
    torch.bfloat16: (3e-2, 2e-2),
}
# Assume that last N batches are no LoRA case
NO_LORA_BATCHES = [2]

@pytest.mark.parametrize("m", TENSOR_SIZES)
@pytest.mark.parametrize("n", TENSOR_SIZES)
@pytest.mark.parametrize("k", BATCH_SIZES)
@pytest.mark.parametrize("rank", RANKS)
@pytest.mark.parametrize("dtype", DTYPES)
@pytest.mark.parametrize("N", NO_LORA_BATCHES)
def test_apply_lora(m, n, k, rank, dtype, N) -> None:
    manager = DummyLoRAManager()

    module_name = "module"
    weight = torch.rand([m, n], device="hpu", dtype=dtype)

    manager.init_random_lora(module_name, weight, rank=rank)
    lora = manager.get_module_lora(module_name)

    input = torch.rand(k, n, device="hpu", dtype=dtype)
    expected = input @ lora.lora_a @ lora.lora_b * lora.scaling
    expected[-N:, :] = torch.zeros(N, expected.shape[1])

    lora_a_stack = torch.zeros(8,
                               1,
                               lora.lora_a.shape[1],
                               lora.lora_a.shape[0],
                               device="hpu",
                               dtype=dtype)
    lora_b_stack = torch.zeros(8,
                               1,
                               lora.lora_b.shape[1],
                               lora.lora_b.shape[0],
                               device="hpu",
                               dtype=dtype)
    for i in range(lora_a_stack.shape[0] - N):
        lora_a_stack[i][0] = lora.lora_a.T
        lora_b_stack[i][0] = (lora.lora_b * lora.scaling).T

    output = torch.zeros(k, m, device="hpu", dtype=dtype)
    indices = torch.randint(0, lora_a_stack.shape[0]-N, (len(input), ), device="hpu")
    indices[-N:] = -1
    _apply_lora(
        input, lora_a_stack, lora_b_stack, indices, output)
    rtol, atol = TOLERANCES[dtype]
    assert torch.allclose(expected, output, rtol=rtol, atol=atol)

    output[:] = 0
    indices = torch.full((len(input), ), -1, device="hpu")
    _apply_lora(
        input, lora_a_stack, lora_b_stack, indices, output)
    assert torch.allclose(torch.zeros_like(output), output)

    manager.reset_lora()


@pytest.mark.parametrize("m", TENSOR_SIZES)
@pytest.mark.parametrize("n", TENSOR_SIZES)
@pytest.mark.parametrize("k", BATCH_SIZES)
@pytest.mark.parametrize("rank", RANKS)
@pytest.mark.parametrize("dtype", DTYPES)
@pytest.mark.parametrize("N", NO_LORA_BATCHES)
def test_apply_lora_packed_2slice(m, n, k, rank, dtype, N) -> None:
    if m % 2 != 0:
        pytest.skip("m must be divisible by 2")
    if m // 2 not in TENSOR_SIZES:
        pytest.skip("m//2 must be in TENSOR_SIZES")

    manager = DummyLoRAManager()

    module_name = "module"
    weight = torch.rand([m // 2, n], device="hpu", dtype=dtype)

    manager.init_random_lora(module_name + "1", weight, rank=rank)
    lora_1 = manager.get_module_lora(module_name + "1")
    manager.init_random_lora(module_name + "2", weight, rank=rank)
    lora_2 = manager.get_module_lora(module_name + "2")

    input = torch.rand(k, n, device="hpu", dtype=dtype)
    expected = torch.cat([
        input @ lora_1.lora_a @ lora_1.lora_b * lora_1.scaling,
        input @ lora_2.lora_a @ lora_2.lora_b * lora_2.scaling
    ],
                         dim=1)
    expected[-N:, :] = torch.zeros(N, expected.shape[1])

    lora_a_stacks = [
        torch.zeros(8,
                    1,
                    lora_1.lora_a.shape[1],
                    lora_1.lora_a.shape[0],
                    device="hpu",
                    dtype=dtype) for i in range(2)
    ]
    lora_b_stacks = [
        torch.zeros(8,
                    1,
                    lora_1.lora_b.shape[1],
                    lora_1.lora_b.shape[0],
                    device="hpu",
                    dtype=dtype) for i in range(2)
    ]
    for i in range(lora_a_stacks[0].shape[0] - N):
        lora_a_stacks[0][i][0] = lora_1.lora_a.T
        lora_b_stacks[0][i][0] = (lora_1.lora_b * lora_1.scaling).T
        lora_a_stacks[1][i][0] = lora_2.lora_a.T
        lora_b_stacks[1][i][0] = (lora_2.lora_b * lora_2.scaling).T

    output = torch.zeros(k, m, device="hpu", dtype=dtype)
    indices = torch.randint(0, lora_a_stacks[0].shape[0]-N, (len(input), ), device="hpu")
    indices[-N:] = -1
    _apply_lora_packed_nslice(
        input, lora_a_stacks, lora_b_stacks,
        indices, output, (m // 2, m // 2))

    rtol, atol = TOLERANCES[dtype]
    assert torch.allclose(expected, output, rtol=rtol, atol=atol)

    output[:] = 0
    indices = torch.full((len(input), ), -1, device="hpu")
    _apply_lora_packed_nslice(
        input, lora_a_stacks, lora_b_stacks,
        indices, output, (m // 2, m // 2))
    assert torch.allclose(torch.zeros_like(output), output)

    manager.reset_lora()


@pytest.mark.parametrize("qkv", QKV_TENSOR_SIZES)
@pytest.mark.parametrize("n", TENSOR_SIZES)
@pytest.mark.parametrize("k", BATCH_SIZES)
@pytest.mark.parametrize("rank", RANKS)
@pytest.mark.parametrize("dtype", DTYPES)
@pytest.mark.parametrize("N", NO_LORA_BATCHES)
def test_apply_lora_packed_3slice(qkv, n, k, rank, dtype, N) -> None:
    manager = DummyLoRAManager()

    module_name = "module"
    weight_q = torch.empty(qkv[0], n, device="hpu", dtype=dtype)
    weight_kv = torch.empty(qkv[1], n, device="hpu", dtype=dtype)

    manager.init_random_lora(module_name + "q", weight_q, rank=rank)
    lora_q = manager.get_module_lora(module_name + "q")
    manager.init_random_lora(module_name + "k", weight_kv, rank=rank)
    lora_k = manager.get_module_lora(module_name + "k")
    manager.init_random_lora(module_name + "v", weight_kv, rank=rank)
    lora_v = manager.get_module_lora(module_name + "v")

    input = torch.rand(k, n, device="hpu", dtype=dtype)
    N = 1 # Assume that last N batches are no LoRA case
    expected = torch.cat([
        input @ lora_q.lora_a @ lora_q.lora_b * lora_q.scaling,
        input @ lora_k.lora_a @ lora_k.lora_b * lora_k.scaling,
        input @ lora_v.lora_a @ lora_v.lora_b * lora_v.scaling
    ],
                         dim=1)
    expected[-N:, :] = torch.zeros(N, expected.shape[1])

    lora_a_stacks = [
        torch.zeros(8,
                    1,
                    lora_q.lora_a.shape[1],
                    lora_q.lora_a.shape[0],
                    device="hpu",
                    dtype=dtype)
    ] + [
        torch.zeros(8,
                    1,
                    lora_k.lora_a.shape[1],
                    lora_k.lora_a.shape[0],
                    device="hpu",
                    dtype=dtype) for i in range(2)
    ]
    lora_b_stacks = [
        torch.zeros(8,
                    1,
                    lora_q.lora_b.shape[1],
                    lora_q.lora_b.shape[0],
                    device="hpu",
                    dtype=dtype)
    ] + [
        torch.zeros(8,
                    1,
                    lora_k.lora_b.shape[1],
                    lora_k.lora_b.shape[0],
                    device="hpu",
                    dtype=dtype) for i in range(2)
    ]
    for i in range(lora_a_stacks[0].shape[0] - N):
        lora_a_stacks[0][i][0] = lora_q.lora_a.T
        lora_b_stacks[0][i][0] = (lora_q.lora_b * lora_q.scaling).T
        lora_a_stacks[1][i][0] = lora_k.lora_a.T
        lora_b_stacks[1][i][0] = (lora_k.lora_b * lora_k.scaling).T
        lora_a_stacks[2][i][0] = lora_v.lora_a.T
        lora_b_stacks[2][i][0] = (lora_v.lora_b * lora_v.scaling).T

    output = torch.zeros(k, sum(qkv), device="hpu", dtype=dtype)
    indices = torch.randint(0, lora_a_stacks[0].shape[0] - N, (len(input), ), device="hpu")
    indices[-N:] = -1
    _apply_lora_packed_nslice(
        input, lora_a_stacks, lora_b_stacks,
        indices, output, (qkv[0], qkv[1], qkv[2]))

    rtol, atol = TOLERANCES[dtype]
    # import pdb; pdb.set_trace()
    assert torch.allclose(expected, output, rtol=rtol, atol=atol)

    output[:] = 0
    indices = torch.full((len(input), ), -1, device="hpu")
    _apply_lora_packed_nslice(
        input, lora_a_stacks, lora_b_stacks,
        indices, output, (qkv[0], qkv[1], qkv[2]))
    assert torch.allclose(torch.zeros_like(output), output)

    manager.reset_lora()
