import argparse
import random
import time
from typing import Optional, List, Tuple, Union
import csv

import torch

from xformers.ops import fmha
from xformers.attn_bias_utils import create_attn_bias

from vllm import _custom_ops as ops
from vllm.utils import STR_DTYPE_TO_TORCH_DTYPE

NUM_BLOCKS = 1024
PARTITION_SIZE = 512


def _generate_random_fp8(
    tensor: torch.Tensor,
    low: float,
    high: float,
) -> None:
    # NOTE(zhaoyang): Due to NaN and Inf representation for fp8 data type,
    # it may occur Inf or NaN if we directly use torch.randint
    # to generate random data for fp8 data.
    # For example, s.11111.00 in fp8e5m2 format represents Inf.
    #     | E4M3        | E5M2
    #-----|-------------|-------------------
    # Inf | N/A         | s.11111.00
    # NaN | s.1111.111  | s.11111.{01,10,11}
    tensor_tmp = torch.empty_like(tensor, dtype=torch.float16)
    tensor_tmp.uniform_(low, high)
    ops.convert_fp8(tensor, tensor_tmp)
    del tensor_tmp


def get_kv_cache_torch_dtype(
        cache_dtype: Optional[Union[str, torch.dtype]],
        model_dtype: Optional[Union[str, torch.dtype]] = None) -> torch.dtype:
    if isinstance(cache_dtype, str):
        if cache_dtype == "auto":
            if isinstance(model_dtype, str):
                torch_dtype = STR_DTYPE_TO_TORCH_DTYPE[model_dtype]
            elif isinstance(model_dtype, torch.dtype):
                torch_dtype = model_dtype
            else:
                raise ValueError(f"Invalid model dtype: {model_dtype}")
        elif cache_dtype in ["half", "bfloat16", "float"]:
            torch_dtype = STR_DTYPE_TO_TORCH_DTYPE[cache_dtype]
        elif cache_dtype == "fp8":
            torch_dtype = torch.uint8
        else:
            raise ValueError(f"Invalid kv cache dtype: {cache_dtype}")
    elif isinstance(cache_dtype, torch.dtype):
        torch_dtype = cache_dtype
    else:
        raise ValueError(f"Invalid kv cache dtype: {cache_dtype}")
    return torch_dtype


def create_kv_caches_with_random(
    num_layers: int,
    num_seqs: int,
    num_kv_heads: int,
    head_size: int,
    cache_dtype: Optional[Union[str, torch.dtype]],
    model_dtype: Optional[Union[str, torch.dtype]] = None,
    seed: int = 0,
    device: Optional[str] = "cuda",
) -> Tuple[List[torch.Tensor], List[torch.Tensor]]:
    torch.random.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)

    torch_dtype = get_kv_cache_torch_dtype(cache_dtype, model_dtype)

    scale = head_size**-0.5
    key_cache_shape = (num_seqs, num_kv_heads, head_size)
    key_caches: List[torch.Tensor] = []
    for _ in range(num_layers):
        key_cache = torch.empty(size=key_cache_shape,
                                dtype=torch_dtype,
                                device=device)
        if cache_dtype in ["auto", "half", "bfloat16", "float"]:
            key_cache.uniform_(-scale, scale)
        elif cache_dtype == 'fp8':
            _generate_random_fp8(key_cache, -scale, scale)
        else:
            raise ValueError(
                f"Does not support key cache of type {cache_dtype}")
        key_caches.append(key_cache)

    value_cache_shape = (num_seqs, num_kv_heads, head_size)
    value_caches: List[torch.Tensor] = []
    for _ in range(num_layers):
        value_cache = torch.empty(size=value_cache_shape,
                                  dtype=torch_dtype,
                                  device=device)
        if cache_dtype in ["auto", "half", "bfloat16", "float"]:
            value_cache.uniform_(-scale, scale)
        elif cache_dtype == 'fp8':
            _generate_random_fp8(value_cache, -scale, scale)
        else:
            raise ValueError(
                f"Does not support value cache of type {cache_dtype}")
        value_caches.append(value_cache)
    return key_caches, value_caches


def run_cuda_benchmark(version: str, input: tuple, num_iters: int, profile: bool = False) -> float:
    output, query, key_cache, value_cache, num_kv_heads, scale, block_tables, seq_lens, block_size, \
        max_seq_len, alibi_slopes, kv_cache_dtype, exp_sums, max_logits, tmp_output = input

    torch.cuda.synchronize()
    if profile:
        torch.cuda.cudart().cudaProfilerStart()
    start_time = time.perf_counter()

    # Using default kv_scale
    kv_scale = 1.0

    if version == "v2":
        for _ in range(num_iters):
            fd_output = fmha.memory_efficient_attention_forward(
                query,
                key_cache,
                value_cache,
                None,
            )
    else:
        raise ValueError(f"Invalid version: {version}")
    torch.cuda.synchronize()

    end_time = time.perf_counter()
    if profile:
        torch.cuda.cudart().cudaProfilerStart()
    
    bs = query.size(1)
    seq_len = max_seq_len
    heads_q = query.size(2) * query.size(-2)
    heads_kv = key_cache.size(2)
    head_size = query.size(-1)
    type = "MHA" if heads_q == heads_kv else (
        "GQA" if heads_kv != 1 else "MQA")
    duration = round((end_time - start_time) / num_iters * 1e6, 3)

    print(f"{duration} us")

    return (type, bs, seq_len, heads_q, heads_kv, head_size, duration)


@torch.inference_mode()
def main(
    version: str,
    num_seqs_list: List,
    seq_len_list: int,
    num_query_heads_list: int,
    num_kv_heads_list: int,
    head_size: int,
    use_alibi: bool,
    block_size: int,
    dtype: torch.dtype,
    seed: int,
    do_profile: bool,
    device: str = "cuda",
    kv_cache_dtype: Optional[str] = None,
    num_iters: int = 50,
) -> None:
    random.seed(seed)
    torch.random.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)

    input_dict = {}
    # heads
    for i, num_query_heads, num_kv_heads in zip(range(len(num_query_heads_list)), num_query_heads_list, num_kv_heads_list):
        query_key = f"{i}_{num_query_heads}"
        kv_key = f"{i}_{num_kv_heads}"
        input_dict[query_key] = {}
        for seq_len in seq_len_list:  # kv length
            if seq_len not in input_dict[query_key].keys():
                input_dict[query_key][seq_len] = {}
                if kv_key not in input_dict[query_key][seq_len].keys():
                    input_dict[query_key][seq_len][kv_key] = []
            for num_seqs in num_seqs_list:  # batch_size
                scale = float(1.0 / (head_size**0.5))
                query = torch.empty(num_seqs,
                                    num_query_heads,
                                    head_size,
                                    dtype=dtype,
                                    device=device)
                query.uniform_(-scale, scale)

                assert num_query_heads % num_kv_heads == 0
                alibi_slopes = None
                if use_alibi:
                    alibi_slopes = torch.randn(num_query_heads,
                                            dtype=torch.float,
                                            device=device)

                seq_lens = [seq_len for _ in range(num_seqs)]
                max_seq_len = max(seq_lens)
                seq_lens = torch.tensor(seq_lens, dtype=torch.int, device=device)

                # Create the block tables.
                max_num_blocks_per_seq = (max_seq_len + block_size - 1) // block_size
                block_tables = []
                for _ in range(num_seqs):
                    block_table = [
                        random.randint(0, NUM_BLOCKS - 1)
                        for _ in range(max_num_blocks_per_seq)
                    ]
                    block_tables.append(block_table)
                block_tables = torch.tensor(block_tables, dtype=torch.int, device=device)

                # Create the KV cache.
                key_caches, value_caches = create_kv_caches_with_random(1,
                                                                        num_seqs,
                                                                        num_kv_heads,
                                                                        head_size,
                                                                        kv_cache_dtype,
                                                                        dtype,
                                                                        device=device)
                key_cache, value_cache = key_caches[0], value_caches[0]

                # Prepare for the paged attention kernel.
                output = torch.empty_like(query)

                heads_per_group = num_query_heads // num_kv_heads
                query = query.view(1, query.size(0), num_kv_heads, heads_per_group, head_size)
                key_cache = key_cache.view(1, key_cache.size(0), num_kv_heads, 1, head_size)
                value_cache = value_cache.view(1, value_cache.size(0), num_kv_heads, 1, head_size)
                if num_query_heads != num_kv_heads:
                    key_cache = key_cache.expand(-1, -1, -1, heads_per_group, -1)
                    value_cache = value_cache.expand(-1, -1, -1, heads_per_group, -1)

                attn_bias = create_attn_bias(
                                None,
                                batch_size=query.size(1),
                                num_heads=num_query_heads,
                                num_heads_groups=heads_per_group,
                                q_len=1,
                                kv_len=max_seq_len,
                                dtype=dtype,
                                device=device,
                                requires_grad=False,
                                fmt="BMHK",
                                op=fmha.decoder.FwOp,
                            )

                input_dict[query_key][seq_len][kv_key].append([output, 
                                                               query, 
                                                               key_cache, 
                                                               value_cache,
                                                               num_kv_heads,
                                                               scale,
                                                               block_tables,
                                                               seq_lens,
                                                               block_size,
                                                               max_seq_len,
                                                               alibi_slopes,
                                                               kv_cache_dtype])
                if version == "v2":
                    num_partitions = ((max_seq_len + PARTITION_SIZE - 1) // PARTITION_SIZE)
                    tmp_output = torch.empty(
                        size=(num_seqs, num_query_heads, num_partitions, head_size),
                        dtype=output.dtype,
                        device=output.device,
                    )
                    exp_sums = torch.empty(
                        size=(num_seqs, num_query_heads, num_partitions),
                        dtype=torch.float32,
                        device=output.device,
                    )
                    max_logits = torch.empty_like(exp_sums)
                    input_dict[query_key][seq_len][kv_key][-1].extend([exp_sums, max_logits, tmp_output])

    print(f"Warming up...")
    for q, q_dict in input_dict.items():
        print(f"heads_q={q[2:]}...")
        for seq_len, s_dict in q_dict.items():
            print(f"......seq_len={seq_len}...")
            for k, k_list in s_dict.items():
                print(f".........heads_kv={k[2:]}...")
                for input in k_list:
                    print(f"............bs={input[1].size(1)}...")
                    run_cuda_benchmark(version, input, num_iters=3, profile=False)

    if do_profile:
        print(f"Profiling Benchmark...")
        name = f"FD_V2_BlockSize{block_size}"
        profiler = torch.profiler.profile(
            schedule=torch.profiler.schedule(
                wait=0, warmup=3, active=2, repeat=0),
            activities=[torch.profiler.ProfilerActivity.CPU,
                        torch.profiler.ProfilerActivity.CUDA],
            on_trace_ready=torch.profiler.tensorboard_trace_handler(
                name, use_gzip=True),
            with_stack=True, with_modules=True, record_shapes=True, profile_memory=True)
        profiler.start()
        for i in range(num_iters):
            print(f"Iteration_{i} Start")
            for q, q_dict in input_dict.items():
                print(f"heads_q={q[2:]}...")
                for seq_len, s_dict in q_dict.items():
                    print(f"......seq_len={seq_len}...")
                    for k, k_list in s_dict.items():
                        print(f".........heads_kv={k[2:]}...")
                        for input in k_list:
                            print(f"............bs={input[1].size(1)}...")
                            run_cuda_benchmark(
                                version, input, num_iters=1, profile=False)
            profiler.step()
            print(f"Iteration_{i} Stop")
        profiler.stop()
        print(
            f"Profiling Benchmark Finished! Please check profiles stored in {name}")
    else:
        print(f"Pure Benchmark...")
        item_list = [["Type", "Dtype", "BS", "Q_Length",
                      "KV_Length", "HeadDim", "Q_Head", "KV_Head", "A100 (us)"]]
        for q, q_dict in input_dict.items():
            print(f"heads_q={q[2:]}...")
            for seq_len, s_dict in q_dict.items():
                print(f"......seq_len={seq_len}...")
                for k, k_list in s_dict.items():
                    print(f".........heads_kv={k[2:]}...")
                    for input in k_list:
                        print(f"............bs={input[1].size(1)}...")
                        type, bs, seq_len, heads_q, heads_kv, head_size, duration = \
                            run_cuda_benchmark(version, input, num_iters=100, profile=False)
                        item_list.append(
                            [type, "FP16/BF16", bs, 1, seq_len, head_size, heads_q, heads_kv, duration])
                        print(item_list[-1])
        path = f"./benchmark_gpu_fa_{version}.csv"
        with open(path, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerows(item_list)
        print(f"Pure Benchmark Finished! Please check data stored in {path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Benchmark the paged attention kernel.")
    parser.add_argument("--version",
                        type=str,
                        choices=["v1", "v2"],
                        default="v2")
    parser.add_argument("--seq-len-list",
                        nargs='+',
                        type=int,
                        default=[512, 1024, 2048, 4096, 8192])
    parser.add_argument("--num-query-heads-list",
                        nargs='+',
                        type=int,
                        default=[32, 32, 32, 32, 32, 8, 16, 32])
    parser.add_argument("--num-kv-heads-list",
                        nargs='+',
                        type=int,
                        default=[32, 2, 4, 8, 16, 1, 1, 1])
    parser.add_argument("--batch-size-list",
                        nargs='+',
                        type=int,
                        default=[1, 4, 16, 64, 256])
    parser.add_argument("--head-size",
                        type=int,
                        choices=[64, 80, 96, 112, 128, 192, 256],
                        default=128)
    parser.add_argument("--block-size", type=int, choices=[16, 32], default=16)
    parser.add_argument("--use-alibi", action="store_true")
    parser.add_argument("--dtype",
                        type=str,
                        choices=["half", "bfloat16", "float"],
                        default="half")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument(
        "--kv-cache-dtype",
        type=str,
        choices=["auto", "fp8", "fp8_e5m2", "fp8_e4m3"],
        default="auto",
        help="Data type for kv cache storage. If 'auto', will use model "
        "data type. CUDA 11.8+ supports fp8 (=fp8_e4m3) and fp8_e5m2. "
        "ROCm (AMD GPU) supports fp8 (=fp8_e4m3)")
    args = parser.parse_args()
    print(args)

    for num_query_heads, num_kv_heads in zip(args.num_query_heads_list, args.num_kv_heads_list):
        if num_query_heads % num_kv_heads != 0:
            raise ValueError("num_query_heads must be divisible by num_kv_heads")
    main(
        version=args.version,
        num_seqs_list=args.batch_size_list,
        seq_len_list=args.seq_len_list,
        num_query_heads_list=args.num_query_heads_list,
        num_kv_heads_list=args.num_kv_heads_list,
        head_size=args.head_size,
        block_size=args.block_size,
        use_alibi=args.use_alibi,
        dtype=STR_DTYPE_TO_TORCH_DTYPE[args.dtype],
        seed=args.seed,
        do_profile=args.profile,
        kv_cache_dtype=args.kv_cache_dtype,
    )