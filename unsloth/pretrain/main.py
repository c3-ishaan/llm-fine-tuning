import mlflow
from unsloth import FastLanguageModel
from transformers import TrainingArguments
from unsloth import is_bfloat16_supported
from unsloth import UnslothTrainer, UnslothTrainingArguments
import torch
from transformers.integrations import MLflowCallback
from transformers import TrainingArguments
from unsloth import is_bfloat16_supported
from unsloth import UnslothTrainer, UnslothTrainingArguments
from datasets import load_dataset
import os
import argparse
import math



from transformers import TrainerCallback  
import mlflow  

def parse_args():
    # setup arg parser
    parser = argparse.ArgumentParser()


    # add arguments
    parser.add_argument("--mounted_data_folder", type=str)
    parser.add_argument("--learning_rate", type=float, default=5e-5)
    parser.add_argument("--model_name", type=str)
    parser.add_argument("--trained_model", type=str, default="trained_model")

    # parse args
    args = parser.parse_args()

    # return args
    return args


class LAMA2Predict(mlflow.pyfunc.PythonModel):
  def __init__(self, model_name):
    self.model_name = model_name
  def load_context(self, context):
    device_map = {"": 0}
    artifact_path = f"{self.model_name}/artifacts/trained_model"
    model = AutoModelForCausalLM.from_pretrained(
        artifact_path,
        local_files_only=True,
        low_cpu_mem_usage=True,
        return_dict=True,
        torch_dtype=torch.float16,
        device_map=device_map,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        artifact_path,
        local_files_only=True,
        device_map=device_map
    )
    model.eval()
    self.conversation_agent = ConversationalPipeline(model=model, tokenizer=tokenizer)

  def predict(self, context, data,**kwargs): 
    TEMPERATURE_KEY = "temperature"
    MAX_GEN_LEN_KEY = "max_gen_len"
    DO_SAMPLE_KEY = "do_sample"
    MAX_NEW_TOKENS_KEY = "max_new_tokens"
    MAX_LENGTH_KEY = "max_length"
    TOP_P_KEY = "top_p"
    B_SYS, E_SYS = "<<SYS>>\n", "\n<</SYS>>\n\n"
    
    if isinstance(data, pd.DataFrame):
        data = data[data.columns[0]].tolist()

    addn_args = kwargs.get("addn_args", {})
    max_gen_len = addn_args.pop(MAX_GEN_LEN_KEY, 256)
    addn_args[MAX_NEW_TOKENS_KEY] = addn_args.get(MAX_NEW_TOKENS_KEY, max_gen_len)
    addn_args[MAX_LENGTH_KEY] = addn_args.get(MAX_LENGTH_KEY, 4096)
    addn_args[TEMPERATURE_KEY] = addn_args.get(TEMPERATURE_KEY, 0.9)
    addn_args[TOP_P_KEY] = addn_args.get(TOP_P_KEY, 0.6)
    addn_args[DO_SAMPLE_KEY] = addn_args.get(DO_SAMPLE_KEY, True)

    
    conv_arr = data
    # validations
    assert len(conv_arr) > 0
    assert conv_arr[-1]["role"] == "user"
    next_turn = "system" if conv_arr[0]["role"] == "system" else "user"
    # Build conversation
    conversation = Conversation()
    
    for i, conv in enumerate(conv_arr):
        if conv["role"] == "system":
            assert next_turn == "system", "System prompts can only be set at the start of the conversation"
            next_turn = "user"
            conversation.add_user_input(B_SYS + conv_arr[0]["content"].strip() + E_SYS)
            conversation.mark_processed()
        if conv["role"] == "assistant":
            assert next_turn == "assistant", "Invalid Turn. Expected user input"
            next_turn = "user"
            conversation.append_response(conv["content"].strip())
        elif conv["role"] == "user":
            assert next_turn == "user", "Invalid Turn. Expected assistant input"
            next_turn = "assistant"
            conversation.add_user_input(conv["content"].strip())
            if i != len(conv_arr[0:]) - 1:
                conversation.mark_processed()
    result = self.conversation_agent(conversation, use_cache=True, **addn_args)
    return result.generated_responses[-1]


class MlflowLoggingCallback(TrainerCallback):  
    def on_log(self, args, state, control, logs=None, **kwargs):  
        # Log metrics to MLflow  
        if logs is not None:  
            mlflow.log_metrics(logs, step=state.global_step)  
            mlflow.log_metric('epoch', state.epoch)  



def main(args):
    model_name = args.model_name
    learning_rate = args.learning_rate
    print("content of the folder ", os.listdir(args.mounted_data_folder))
    trained_model = args.trained_model
    #save your train model to this folder to persist to job storage in cloud
    print("content of the trained_model folder ", os.listdir(args.trained_model))

    max_seq_length = 2048 # Choose any! We auto support RoPE Scaling internally!
    dtype = None # None for auto detection. Float16 for Tesla T4, V100, Bfloat16 for Ampere+
    load_in_4bit = True # Use 4bit quantization to reduce memory usage. Can be False.
    MODEL_NAME = "unsloth/Meta-Llama-3.1-8B"


    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name = MODEL_NAME, # Choose ANY! eg teknium/OpenHermes-2.5-Mistral-7B
        max_seq_length = max_seq_length,
        dtype = dtype,
        load_in_4bit = load_in_4bit,
        # token = "hf_...", # use one if using gated models like meta-llama/Llama-2-7b-hf
    )

    model = FastLanguageModel.get_peft_model(
        model,
        r = 64, # Choose any number > 0 ! Suggested 8, 16, 32, 64, 128
        target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj",
                        # "embed_tokens", "lm_head",
                        ], # Add for continual pretraining
        lora_alpha = 16,
        lora_dropout = 0.05, # Supports any, but = 0 is optimized
        bias = "none",    # Supports any, but = "none" is optimized
        # [NEW] "unsloth" uses 30% less VRAM, fits 2x larger batch sizes!
        use_gradient_checkpointing = "unsloth", # True or "unsloth" for very long context
        random_state = 3407,
        # use_rslora = True,   # We support rank stabilized LoRA
        loftq_config = None, # And LoftQ
    
    )



    EOS_TOKEN = tokenizer.eos_token # Must add EOS_TOKEN
    def formatting_prompts_func(examples):
        inputs       = examples["inputs"]
        outputs      = examples["targets"]
        texts = []
        for inp, output in zip(inputs, outputs):  
            # Must add EOS_TOKEN, otherwise your generation will go on forever!
            text = output  + EOS_TOKEN
            texts.append(text)
        return { "text" : texts, }
    pass

    DATASET_TRAIN_PATH = os.path.join(args.mounted_data_folder, "joined/train")
    DATASET_VAL_PATH = os.path.join(args.mounted_data_folder, "joined/validation")
    print("dataset train path:", DATASET_TRAIN_PATH)
    print("dataset val path:", DATASET_VAL_PATH)

    from datasets import load_dataset, load_from_disk
    dataset_train = load_from_disk(DATASET_TRAIN_PATH)
    dataset_train = dataset_train.map(formatting_prompts_func, batched = True,)
    dataset_train = dataset_train.train_test_split(train_size = 0.01)["train"]

    dataset_val = load_from_disk(DATASET_VAL_PATH)
    dataset_val = dataset_val.map(formatting_prompts_func, batched = True,)
    dataset_val = dataset_val.train_test_split(train_size = 0.01)["train"]



    from trl import SFTTrainer
    from transformers import TrainingArguments
    from unsloth import is_bfloat16_supported
    from unsloth import UnslothTrainer, UnslothTrainingArguments

    trainer = UnslothTrainer(
        model = model,
        tokenizer = tokenizer,
        train_dataset = dataset_train,
        eval_dataset = dataset_val,
        dataset_text_field = "text",
        max_seq_length = max_seq_length,
        dataset_num_proc = 8,
        packing = False, # Can make training 5x faster for short sequences.
        callbacks=[MlflowLoggingCallback()],

        args = UnslothTrainingArguments(
            per_device_train_batch_size = 8,
            gradient_accumulation_steps = 2,
            # warmup_steps = 5, # apply as needed
            # num_train_epochs = 3, 
            max_steps = 100, # set this to define steps instead of epochs
            learning_rate = 1e-4,
            # embedding_learning_rate = 1e-6, # set this if 'embed_tokens' and 'lm_head' modules were included in target_modules
            fp16 = not is_bfloat16_supported(),
            bf16 = is_bfloat16_supported(),
            logging_steps = 10,
            optim = "adamw_8bit",
            # weight_decay = 0.01,
            lr_scheduler_type = "linear",
            seed = 3407,
            save_steps = 10,
            do_eval = True,
            eval_steps = 10,
            eval_on_start = True,
            eval_strategy = "steps",
            output_dir = "shasaj_output_dir",
            # report_to = "wandb",
        ),
    )


    #@title Show current memory stats
    gpu_stats = torch.cuda.get_device_properties(0)
    start_gpu_memory = round(torch.cuda.max_memory_reserved() / 1024 / 1024 / 1024, 3)
    max_memory = round(gpu_stats.total_memory / 1024 / 1024 / 1024, 3)
    print(f"GPU = {gpu_stats.name}. Max memory = {max_memory} GB.")
    print(f"{start_gpu_memory} GB of memory reserved.")
    trainer.remove_callback(MLflowCallback)
    model_output_dir = "trained_model"
    model_artifact_path = model_name
    with mlflow.start_run() as run:
        trainer_stats = trainer.train()
        eval_results = trainer.evaluate()
        mlflow.log_metric("perplexity",math.exp(eval_results['eval_loss']))

        model.save_pretrained(model_output_dir)
        tokenizer.save_pretrained(model_output_dir)
            # os.environ["AZUREML_ARTIFACTS_DEFAULT_TIMEOUT"] = "1800" #give time for model to be registered
            # mlflow.pyfunc.log_model(artifacts={pipeline_artifact_name: model_output_dir}, artifact_path=model_artifact_path, python_model=LAMA2Predict(model_name))
            # model_uri = f"runs:/{run.info.run_id}/{model_artifact_path}"
            # mlflow.register_model(model_uri, name = model_name,await_registration_for=1800)



if __name__ == "__main__":
    # parse args
    args = parse_args()

    # run main function
    main(args)