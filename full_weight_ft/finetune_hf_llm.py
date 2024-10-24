import argparse
import functools
import json
import math
import os
from pathlib import Path
import pandas as pd
import re
import tempfile
import time
import tree
from typing import Tuple
from datasets import Dataset
from torch.utils.data import DataLoader

import mlflow
try:
    import deepspeed  # noqa: F401
except ImportError as e:
    raise RuntimeError(
        "Please install deepspeed with `pip install --user deepspeed`."
    ) from e

from accelerate import Accelerator, DeepSpeedPlugin
from accelerate.utils import DummyOptim, DummyScheduler, set_seed
import torch
import torch.nn as nn
import tqdm
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    get_linear_schedule_with_warmup,
)


# from utils import (
#     get_checkpoint_and_refs_dir,
#     get_mirror_link,
#     download_model,
#     get_download_path,
# )


OPTIM_BETAS = (0.9, 0.999)
OPTIM_EPS = 1e-8
OPTIM_WEIGHT_DECAY = 0.0


def get_number_of_params(model: nn.Module):
    state_dict = model.state_dict()
    return sum(p.numel() for p in state_dict.values())


def collate_fn(batch, tokenizer, block_size):
    batch = [item["input"] for item in batch]
    out_batch = tokenizer(
        batch,
        padding="max_length",
        max_length=block_size,
        truncation=True,
        return_tensors="pt",
    )
    out_batch["labels"] = out_batch["input_ids"].clone()

    # out_batch = tree.map_structure(lambda x: x.to(device), out_batch)

    return out_batch



def get_tokenizer(pretrained_path, special_tokens):

    # Context for legacy=True: https://github.com/huggingface/transformers/issues/25176
    tokenizer = AutoTokenizer.from_pretrained(pretrained_path, legacy=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.add_tokens(special_tokens, special_tokens=True)

    return tokenizer


def evaluate(
    *, model, eval_ds, accelerator, bsize, ds_kwargs, as_test: bool = False
) -> Tuple[float, float]:
    model.eval()
    losses = []
    eval_dataloader = DataLoader(eval_ds, batch_size=bsize,**ds_kwargs)
    eval_dataloader = accelerator.prepare(eval_dataloader)

    # eval_dataloader = eval_ds.iter_torch_batches(batch_size=bsize, **ds_kwargs)
    eval_ds_len = len(eval_ds)
    for step, batch in enumerate(eval_dataloader):
    
        with torch.no_grad():
            outputs = model(**batch)

        loss = outputs.loss
        # The tensors are gathered by concatenating them on the first dimension, so we
        # add a new dimension to the scalar loss to get a tensor of shape (K,) for K
        # workers.
        losses.append(accelerator.gather(loss[None]))

        if as_test:
            break

    # We stack losses so that we have a tensor of shape (T, K) where T is the number of
    # steps and K is the number of workers.
    losses = torch.stack(losses)
    try:
        eval_loss = torch.mean(losses).item()
        perplexity = math.exp(eval_loss)
    except OverflowError:
        perplexity = float("inf")
    return perplexity, eval_loss


def _test_tokenizer(pretrained_path):
    # This function tests that adding special tokens does not
    # result in un-expected tokenization
    # Context: https://github.com/huggingface/transformers/issues/25176
    tokenizer = get_tokenizer(pretrained_path, special_tokens=["<REPR_END>"])
    testoutput = tokenizer("<REPR_END>inform")["input_ids"]
    expected = tokenizer("inform")["input_ids"]
    assert testoutput[-1] == expected[-1], (
        "The tokenizer is not working as expected with special tokens, "
        f"testoutput={testoutput}, expected={expected}"
    )


def checkpoint_model(
    checkpoint_folder, ckpt_id, model, epoch, last_global_step, **kwargs
):
    """Utility function for checkpointing model + optimizer dictionaries
    The main purpose for this is to be able to resume training from that instant again.
    """
    checkpoint_state_dict = {
        "epoch": epoch,
        "last_global_step": last_global_step,
    }
    # Add extra kwargs too
    checkpoint_state_dict.update(kwargs)

    # In here model will be a DeepspeedEngine object
    model.save_checkpoint(checkpoint_folder, ckpt_id, checkpoint_state_dict)
    status_msg = (
        f"checkpointing: checkpoint_folder={checkpoint_folder}, ckpt_id={ckpt_id}"
    )
    print(status_msg)


def training_function(kwargs: dict):
    print("training_function called")

    config = kwargs["config"]
    args = argparse.Namespace(**kwargs["args"])
    special_tokens = kwargs.get("special_tokens", [])
    # model_id = config["model_name"]

    lr = config["lr"]
    num_epochs = int(config["num_epochs"])
    seed = int(config["seed"])
    batch_size = int(config["batch_size"])
    gradient_accumulation_steps = int(config["gradient_accumulation_steps"])

    # Get deepspeed config to setup the batch size per device
    ds_plugin = config["ds_plugin"]
    ds_plugin.hf_ds_config.config["train_micro_batch_size_per_gpu"] = batch_size

    # Initialize accelerator
    accelerator = Accelerator(
        deepspeed_plugin=ds_plugin,
        gradient_accumulation_steps=gradient_accumulation_steps,
        mixed_precision=args.mx,
    )

    set_seed(seed)

    datasets = config["datasets"]
    train_ds = datasets["train"]
    valid_ds = datasets["valid"]

    pretrained_path = os.path.join(config["model_dir"],"data", "model")
    # _test_tokenizer(args.model_name)
    tokenizer = get_tokenizer(pretrained_path, special_tokens=special_tokens)
    collate_partial = functools.partial(
        collate_fn,
        tokenizer=tokenizer,
        block_size=config["block_size"],
        # device=accelerator.device,
    )
    train_dataloader = DataLoader(train_ds, batch_size=batch_size, collate_fn=collate_partial)

    train_ds_len = len(train_ds)

    
    print(f"Loading model from {pretrained_path} ...")
    s = time.time()
    model = AutoModelForCausalLM.from_pretrained(
        pretrained_path,
        trust_remote_code=True,
        torch_dtype=torch.bfloat16,
        # `use_cache=True` is incompatible with gradient checkpointing.
        use_cache=False,
    )
    print(f"Done loading model in {time.time() - s} seconds.")
    model.resize_token_embeddings(len(tokenizer))
    print("Model initialized with pretrained weights. Training starting...")
    if not args.no_grad_ckpt:
        model.gradient_checkpointing_enable()

    optimizer_cls = (
        torch.optim.AdamW
        if accelerator.state.deepspeed_plugin is None
        or "optimizer" not in accelerator.state.deepspeed_plugin.deepspeed_config
        else DummyOptim
    )

    optimizer = optimizer_cls(
        model.parameters(),
        lr=lr,
        betas=OPTIM_BETAS,
        weight_decay=OPTIM_WEIGHT_DECAY,
        eps=OPTIM_EPS,
    )

    # Instantiate scheduler
    # Creates Dummy Scheduler if `scheduler` was specified in the config file
    # else, creates `args.lr_scheduler_type` Scheduler
    # get train and valid dataset lengths

    if (
        accelerator.state.deepspeed_plugin is None
        or "scheduler" not in accelerator.state.deepspeed_plugin.deepspeed_config
    ):
        lr_scheduler = get_linear_schedule_with_warmup(
            optimizer=optimizer,
            num_warmup_steps=100,
            num_training_steps=(
                (train_ds_len * num_epochs) // gradient_accumulation_steps
            ),
        )
    else:
        lr_scheduler = DummyScheduler(
            optimizer,
            total_num_steps=(train_ds_len * num_epochs) // gradient_accumulation_steps,
            warmup_num_steps=100,
        )

    # Prepare everything
    # There is no specific order to remember, we just need to unpack the objects in the
    # same order we gave them to the prepare method.
    s = time.time()
    model, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(model, optimizer, train_dataloader, lr_scheduler)
    print(f"Prepare done in {time.time() - s} seconds.")

    # Now we train the model
    if accelerator.is_main_process:
        print("Starting training ...")
        print("Number of batches on main process", train_ds_len // batch_size)
    with mlflow.start_run() as run:
        for epoch in range(num_epochs):

            fwd_time_sum, bwd_time_sum, optim_step_time_sum = 0, 0, 0
            s_epoch = time.time()
            model.train()
            loss_sum = torch.tensor(0.0).to(accelerator.device)

            # train_dataloader = train_ds.iter_torch_batches(
            #     batch_size=batch_size,
            #     collate_fn=collate_partial,
            # )

            for step, batch in enumerate(train_dataloader):

                # We could avoid this line since we set the accelerator with
                # `device_placement=True`.
                with accelerator.accumulate(model):
                    s_fwd = time.time()
                    outputs = model(**batch)
                    loss = outputs.loss
                    loss_sum += loss
                    e_fwd = time.time()
                    fwd_time = e_fwd - s_fwd
                    fwd_time_sum += fwd_time
                    s_bwd = time.time()
                    accelerator.backward(loss)
                    e_bwd = time.time()
                    bwd_time = e_bwd - s_bwd
                    bwd_time_sum += bwd_time

                    s_opt_step = time.time()
                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad()
                    e_opt_step = time.time()
                    optim_step_time_sum += e_opt_step - s_opt_step

                if accelerator.is_main_process:
                    accelerator.print(
                        f"[epoch {epoch} step {step}] "
                        f"loss: {loss.item()} step-time: {e_opt_step - s_fwd}"
                    )

                if config["as_test"]:
                    break

                # as long as this is not the last step report here
                if step != (train_ds_len // batch_size - 1):
                    aggregated_loss = torch.mean(accelerator.gather(loss[None])).item()
                    if accelerator.is_main_process:
                        mlflow.log_metrics(
                            {
                                "epoch": epoch,
                                "iteration": step,
                                "train_loss_batch": aggregated_loss,
                                # "avg_train_loss_epoch": None,
                                # "eval_loss": None,
                                # "perplexity": None,
                                "num_iterations": step + 1,
                                # "train_time_per_epoch": None,
                                # "eval_time_per_epoch": None,
                                "fwd_time": fwd_time,
                                "bwd_time": bwd_time,
                                # "avg_fwd_time_per_epoch": None,
                                # "avg_bwd_time_per_epoch": None,
                                "learning_rate": lr_scheduler.get_lr()[0],
                            }
                        )

            e_epoch = time.time()
            accelerator.print("Train time per epoch: ", e_epoch - s_epoch)

            eval_s_epoch = time.time()
            print("Running evaluation ...")
            perplex, eloss = evaluate(
                model=model,
                eval_ds=valid_ds,
                accelerator=accelerator,
                bsize=config["eval_batch_size"],
                ds_kwargs={"collate_fn": collate_partial},
                as_test=config["as_test"],
            )
            accelerator.print("Eval result loss", eloss)
            accelerator.print("Eval perplex", perplex)

            eval_e_epoch = time.time()
            accelerator.print("Eval time per epoch: ", eval_e_epoch - eval_s_epoch)
            accelerator.print("avg fwd time: ", fwd_time_sum / (step + 1))
            accelerator.print("avg bwd time: ", bwd_time_sum / (step + 1))
            accelerator.print("avg opt step time: ", optim_step_time_sum / (step + 1))

            metrics = {
                "epoch": epoch,
                "iteration": step,
                "train_loss_batch": loss.item(),
                "avg_train_loss_epoch": loss_sum.item() / (step + 1),
                "eval_loss": eloss,
                "perplexity": perplex,
                "num_iterations": step + 1,
                "train_time_per_epoch": e_epoch - s_epoch,
                "eval_time_per_epoch": eval_e_epoch - eval_s_epoch,
                "fwd_time": fwd_time,
                "bwd_time": bwd_time,
                "avg_fwd_time_per_epoch": fwd_time_sum / (step + 1),
                "avg_bwd_time_per_epoch": bwd_time_sum / (step + 1),
                "learning_rate": lr_scheduler.get_lr()[0],
            }

            with tempfile.TemporaryDirectory() as temp_checkpoint_dir:
                accelerator.print(f"Saving the model locally at {temp_checkpoint_dir}")
                accelerator.wait_for_everyone()

                checkpoint_save_start = time.perf_counter()

                if accelerator.is_main_process:
                    print("Saving tokenizer and config.")
                    tokenizer.save_pretrained(temp_checkpoint_dir)

                accelerator.wait_for_everyone()

                aggregate_on_rank_0 = True
                unwrapped_model = accelerator.unwrap_model(model)
                unwrapped_model.save_pretrained(
                    temp_checkpoint_dir,
                    is_main_process=accelerator.is_main_process,
                    save_function=accelerator.save,
                    safe_serialization=True,
                    state_dict=accelerator.get_state_dict(model),
                )
                accelerator.wait_for_everyone()
                print("Checkpoint save time: ", time.perf_counter() - checkpoint_save_start)

                checkpoint_upload_start = time.perf_counter()

                if accelerator.is_main_process:
                    mlflow.log_metrics(metrics, step=step)

                print(
                    "Checkpoint upload time: ",
                    time.perf_counter() - checkpoint_upload_start,
                )
                print(
                    "Total checkpointing time: ",
                    time.perf_counter() - checkpoint_save_start,
                )


def parse_args():

    parser = argparse.ArgumentParser(description="Simple example of training script.")
    parser.add_argument(
        "--mx",
        type=str,
        default="bf16",
        choices=["no", "fp16", "bf16", "fp8"],
        help="Whether to use mixed precision. Choose"
        "between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >= 1.10."
        "and an Nvidia Ampere GPU.",
    )

    parser.add_argument(
        "--batch-size-per-device",
        "-bs",
        type=int,
        default=16,
        help="Batch size to use per device.",
    )

    parser.add_argument(
        "--eval-batch-size-per-device",
        type=int,
        default=64,
        help="Batch size to use per device (For evaluation).",
    )

    parser.add_argument(
        "--num-devices", "-nd", type=int, default=4, help="Number of devices to use."
    )
    parser.add_argument(
        "--grad_accum", type=int, default=1, help="Gradient accumulation steps."
    )
    parser.add_argument("--train_path", type=str, help="Path to training jsonl file")
    parser.add_argument("--test_path", type=str, help="Path to testing jsonl file")
    parser.add_argument(
        "--special_token_path", type=str, help="Path to token json file"
    )
    parser.add_argument(
        "--no-grad-ckpt",
        action="store_true",
        help="If passed, will not use gradient checkpointing.",
    )
    parser.add_argument("--output_dir", type=str,default="outputs", help="Path to output directory.")
    parser.add_argument(
        "--model_name", default="meta-llama/Llama-2-7b-chat-hf", type=str
    )
    parser.add_argument(
        "--num-epochs", type=int, default=1, help="Number of epochs to train for."
    )
    parser.add_argument(
        "--num-checkpoints-to-keep",
        type=int,
        help=(
            "Number of checkpoints to keep, if None, all checkpoints will be kept, "
            "if set to n>=1, the top n checkpoint with min. evaluation perplexity "
            "will be kept."
        ),
        default=None,
    )
    parser.add_argument("--lr", type=float, default=5e-6, help="Learning rate to use.")

    parser.add_argument(
        "--ctx-len", type=int, default=512, help="Learning rate to use."
    )

    parser.add_argument(
        "--as-test",
        action="store_true",
        help="If passed, will run the script in test mode.",
    )

    parser.add_argument(
        "--ds-config",
        type=str,
        default="./deepspeed_configs/zero_3_llama_2_7b.json",
        help="Deepspeed config json to use.",
    )

    parser.add_argument("--chat_model", type=str, default="False")
    parser.add_argument("--model_dir", type=str)
    parser.add_argument("--trained_model", type=str, default="trained_model")
    # parse args

    args = parser.parse_args()

    return args


def main():

    args = parse_args()

    config = vars(args)
    config.update(
        **{
            "lr": args.lr,
            "num_epochs": args.num_epochs,
            "seed": 42,
            "batch_size": args.batch_size_per_device,
            "gradient_accumulation_steps": args.grad_accum,
            "model_name": args.model_name,
            "block_size": args.ctx_len,
            "eval_batch_size": args.eval_batch_size_per_device,
            "model_dir":args.model_dir,
        }
    )

    # Add deepspeed plugin to the config
    ds_plugin = DeepSpeedPlugin(hf_ds_config=config.get("ds_config"))
    config.update(ds_plugin=ds_plugin)

    os.environ["RAY_AIR_LOCAL_CACHE_DIR"] = args.output_dir


    train_ds = pd.read_json(args.train_path, lines=True).to_dict("records")
    if args.test_path is not None:
        valid_ds = pd.read_json(args.test_path, lines=True).to_dict("records")
    else:
        valid_ds = None

    # json file
    with open(args.special_token_path, "r") as json_file:
        special_tokens = json.load(json_file)["tokens"]

    datasets={"train": train_ds, "valid": valid_ds}
    config.update(datasets=datasets)
    training_function(
        {
            "config": config,
            "args": vars(args),
            "special_tokens": special_tokens,
        }
        )

if __name__ == "__main__":
    main()
