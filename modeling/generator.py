from dataclasses import dataclass
from typing import List, Tuple

import torch
from huggingface_hub import hf_hub_download
from modeling.models import Model
from moshi.models import loaders
from tokenizers.processors import TemplateProcessing
from transformers import AutoTokenizer
from contextlib import ExitStack


@dataclass
class Segment:
    speaker: int
    text: str
    # (num_samples,), sample_rate = 24_000
    audio: torch.Tensor


def load_llama3_tokenizer(tokenizer_name: str = "unsloth/Llama-3.2-1B"):
    """
    https://github.com/huggingface/transformers/issues/22794#issuecomment-2092623992
    """
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    bos = tokenizer.bos_token
    eos = tokenizer.eos_token
    tokenizer._tokenizer.post_processor = TemplateProcessing(
        single=f"{bos}:0 $A:0 {eos}:0",
        pair=f"{bos}:0 $A:0 {eos}:0 {bos}:1 $B:1 {eos}:1",
        special_tokens=[
            (f"{bos}", tokenizer.bos_token_id),
            (f"{eos}", tokenizer.eos_token_id),
        ],
    )

    return tokenizer


class Generator:
    def __init__(
        self,
        model: Model,
        setup_caches: bool = True,
        tokenizer_name: str = "unsloth/Llama-3.2-1B",
    ):
        self._model = model
        if setup_caches:
            self._model.setup_caches(1)

        self._text_tokenizer = load_llama3_tokenizer(tokenizer_name)

        device = next(model.parameters()).device
        mimi_weight = hf_hub_download(loaders.DEFAULT_REPO, loaders.MIMI_NAME)
        mimi = loaders.get_mimi(mimi_weight, device=device)
        mimi.set_num_codebooks(32)
        self._audio_tokenizer = mimi

        self.sample_rate = mimi.sample_rate
        self.device = device

    def _tokenize_text_segment(
        self, text: str, speaker: int
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        frame_tokens = []
        frame_masks = []

        text_tokens = self._text_tokenizer.encode(f"[{speaker}]{text}")
        text_frame = torch.zeros(len(text_tokens), 33).long()
        text_frame_mask = torch.zeros(len(text_tokens), 33).bool()
        text_frame[:, -1] = torch.tensor(text_tokens)
        text_frame_mask[:, -1] = True

        frame_tokens.append(text_frame.to(self.device))
        frame_masks.append(text_frame_mask.to(self.device))

        return torch.cat(frame_tokens, dim=0), torch.cat(frame_masks, dim=0)

    def _tokenize_audio(
        self, audio: torch.Tensor, is_preencoded: bool = False
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        frame_tokens = []
        frame_masks = []

        # (K, T)
        audio = audio.to(self.device)
        if not is_preencoded:
            audio_tokens = self._audio_tokenizer.encode(
                audio.unsqueeze(0).unsqueeze(0)
            )[0]
        else:
            audio_tokens = audio
        # add EOS frame
        eos_frame = torch.zeros(audio_tokens.size(0), 1).to(self.device)
        audio_tokens = torch.cat([audio_tokens, eos_frame], dim=1)

        audio_frame = torch.zeros(audio_tokens.size(1), 33).long().to(self.device)
        audio_frame_mask = torch.zeros(audio_tokens.size(1), 33).bool().to(self.device)
        audio_frame[:, :-1] = audio_tokens.transpose(0, 1)
        audio_frame_mask[:, :-1] = True

        frame_tokens.append(audio_frame)
        frame_masks.append(audio_frame_mask)

        return torch.cat(frame_tokens, dim=0), torch.cat(frame_masks, dim=0)

    def _tokenize_segment(self, segment: Segment) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Returns:
            (seq_len, 33), (seq_len, 33)
        """
        text_tokens, text_masks = self._tokenize_text_segment(
            segment.text, segment.speaker
        )
        audio_tokens, audio_masks = self._tokenize_audio(segment.audio)

        return torch.cat([text_tokens, audio_tokens], dim=0), torch.cat(
            [text_masks, audio_masks], dim=0
        )

    @torch.inference_mode()
    def generate(
        self,
        text: str,
        speaker: int,
        context: List[Segment],
        max_audio_length_ms: float = 15_000,
        temperature: float = 0.9,
        topk: int = 50,
    ) -> torch.Tensor:
        self._model.reset_caches()
        self._audio_tokenizer._stop_streaming()

        max_audio_frames = int(max_audio_length_ms / 80)
        tokens, tokens_mask = [], []
        for segment in context:
            segment_tokens, segment_tokens_mask = self._tokenize_segment(segment)
            tokens.append(segment_tokens)
            tokens_mask.append(segment_tokens_mask)

        gen_segment_tokens, gen_segment_tokens_mask = self._tokenize_text_segment(
            text, speaker
        )
        tokens.append(gen_segment_tokens)
        tokens_mask.append(gen_segment_tokens_mask)

        prompt_tokens = torch.cat(tokens, dim=0).long().to(self.device)
        prompt_tokens_mask = torch.cat(tokens_mask, dim=0).bool().to(self.device)

        samples = []
        curr_tokens = prompt_tokens.unsqueeze(0)
        curr_tokens_mask = prompt_tokens_mask.unsqueeze(0)
        curr_pos = (
            torch.arange(0, prompt_tokens.size(0)).unsqueeze(0).long().to(self.device)
        )

        max_seq_len = 2048 - max_audio_frames
        if curr_tokens.size(1) >= max_seq_len:
            raise ValueError(
                f"Inputs too long, must be below max_seq_len - max_audio_frames: {max_seq_len}"
            )

        self._audio_tokenizer._start_streaming(1, ExitStack())

        for _ in range(max_audio_frames):
            sample = self._model.generate_frame(
                curr_tokens, curr_tokens_mask, curr_pos, temperature, topk
            )
            if torch.all(sample == 0):
                break  # eos

            curr_tokens = torch.cat(
                [sample, torch.zeros(1, 1).long().to(self.device)], dim=1
            ).unsqueeze(1)
            curr_tokens_mask = torch.cat(
                [
                    torch.ones_like(sample).bool(),
                    torch.zeros(1, 1).bool().to(self.device),
                ],
                dim=1,
            ).unsqueeze(1)
            curr_pos = curr_pos[:, -1:] + 1

            frame = (
                self._audio_tokenizer.decode(torch.stack([sample]).permute(1, 2, 0))
                .squeeze(0)
                .squeeze(0)
            )
            yield frame


def load_csm_1b(device: str = "cuda", setup_caches: bool = True) -> Generator:
    model = Model.from_pretrained("sesame/csm-1b")
    model.to(device=device, dtype=torch.bfloat16)
    model.decoder = torch.compile(model.decoder, fullgraph=True, mode="reduce-overhead")

    generator = Generator(model, setup_caches=setup_caches)
    return generator
