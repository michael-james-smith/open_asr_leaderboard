import argparse

import os
import shutil
import torch
import evaluate
import soundfile

from tqdm import tqdm
from normalizer import data_utils
import numpy as np

from nemo.collections.asr.models import ASRModel
import time


wer_metric = evaluate.load("wer")


def main(args):

    DATA_CACHE_DIR = os.path.join(os.getcwd(), "audio_cache")
    DATASET_NAME = args.dataset

    CACHE_DIR = os.path.join(DATA_CACHE_DIR, DATASET_NAME)
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

    if args.device >= 0:
        device = torch.device(f"cuda:{args.device}")
    else:
        device = torch.device("cpu")

    if args.model_id.endswith(".nemo"):
        asr_model = ASRModel.restore_from(args.model_id, map_location=device)
    else:
        asr_model = ASRModel.from_pretrained(args.model_id, map_location=device)  # type: ASRModel
    asr_model.eval()
    asr_model.to(torch.bfloat16)

    dataset = data_utils.load_data(args)

    def download_audio_files(batch):

        # download audio files and write the paths, transcriptions and durations to a manifest file
        audio_paths = []
        durations = []

        for id, sample in zip(batch["id"], batch["audio"]):
            # import ipdb; ipdb.set_trace()
            audio_path = os.path.join(CACHE_DIR, f"{id}.wav")
            if not os.path.exists(audio_path):
                soundfile.write(audio_path, np.float32(sample["array"]), 16_000)
            audio_paths.append(audio_path)
            durations.append(len(sample["array"]) / 16_000)

        
        batch["references"] = batch["norm_text"]
        batch["audio_filepaths"] = audio_paths
        batch["durations"] = durations

        return batch


    if args.max_eval_samples is not None and args.max_eval_samples > 0:
        print(f"Subsampling dataset to first {args.max_eval_samples} samples !")
        dataset = dataset.take(args.max_eval_samples)

    dataset = data_utils.prepare_data(dataset)
    asr_model.cfg.decoding.strategy = "greedy_batched"
    asr_model.change_decoding_strategy(asr_model.cfg.decoding)
    # prepraing the offline dataset
    # dataset = dataset.map(lambda x: x, batch_size=args.batch_size, batched=True)
    
    dataset = dataset.map(download_audio_files, batch_size=args.batch_size, batched=True, remove_columns=["audio"])

    # Write manifest from daraset batch using json and keys audio_filepath, duration, text

    all_data = {
        "audio_filepaths": [],
        "durations": [],
        "references": [],
    }

    data_itr = iter(dataset)
    for data in tqdm(data_itr, desc="Downloading Samples"):
        # import ipdb; ipdb.set_trace()
        for key in all_data:
            all_data[key].append(data[key])
    
    # Sort audio_filepaths and references based on durations values
    sorted_indices = sorted(range(len(all_data["durations"])), key=lambda k: all_data["durations"][k], reverse=True)
    all_data["audio_filepaths"] = [all_data["audio_filepaths"][i] for i in sorted_indices]
    all_data["references"] = [all_data["references"][i] for i in sorted_indices]
    all_data["durations"] = [all_data["durations"][i] for i in sorted_indices]
    

    start_time = time.time()
    with torch.cuda.amp.autocast(enabled=False, dtype=torch.bfloat16):
        with torch.inference_mode():
            transcriptions = asr_model.transcribe(all_data["audio_filepaths"], batch_size=args.batch_size, verbose=False, num_workers=1)
    end_time = time.time()

    total_time = end_time - start_time

    # normalize transcriptions with English normalizer
    predictions = [data_utils.normalizer(pred) for pred in transcriptions]

    # Write manifest results (WER and RTFX)
    manifest_path = data_utils.write_manifest(
        all_data["references"],
        predictions,
        args.model_id,
        args.dataset_path,
        args.dataset,
        args.split,
        audio_length=all_data["durations"],
        # transcription_time=all_results["transcription_time"],
    )

    print("Results saved at path:", os.path.abspath(manifest_path))

    wer = wer_metric.compute(references=all_data['references'], predictions=predictions)
    wer = round(100 * wer, 2)

    # transcription_time = sum(all_results["transcription_time"])
    audio_length = sum(all_data["durations"])
    rtfx = audio_length / total_time
    rtfx = round(rtfx, 2)

    print("RTFX:", rtfx)
    print("WER:", wer, "%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--model_id", type=str, required=True, help="Model identifier. Should be loadable with NVIDIA NeMo.",
    )
    parser.add_argument(
        '--dataset_path', type=str, default='esb/datasets', help='Dataset path. By default, it is `esb/datasets`'
    )
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        help="Dataset name. *E.g.* `'librispeech_asr` for the LibriSpeech ASR dataset, or `'common_voice'` for Common Voice. The full list of dataset names "
        "can be found at `https://huggingface.co/datasets/esb/datasets`",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Split of the dataset. *E.g.* `'validation`' for the dev split, or `'test'` for the test split.",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=-1,
        help="The device to run the pipeline on. -1 for CPU (default), 0 for the first GPU and so on.",
    )
    parser.add_argument(
        "--batch_size", type=int, default=32, help="Number of samples to go through each streamed batch.",
    )
    parser.add_argument(
        "--max_eval_samples",
        type=int,
        default=None,
        help="Number of samples to be evaluated. Put a lower number e.g. 64 for testing this script.",
    )
    parser.add_argument(
        "--no-streaming",
        dest='streaming',
        action="store_false",
        help="Choose whether you'd like to download the entire dataset or stream it during the evaluation.",
    )
    args = parser.parse_args()
    parser.set_defaults(streaming=True)

    main(args)
