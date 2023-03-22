import pandas as pd
import numpy as np
from pyannote.core import Annotation, Segment, SlidingWindowFeature, Timeline
from typing import List, Tuple, Optional
import torch
import wave

from whisperx.diarize import Segment


class VADSegmentPipeline:
    def __init__(self, model_name: str, hf_token: str, device: str, chunk_length: int = 30):
        self.device = device
        self.hf_token = hf_token
        self.model_name = model_name
        self.chunk_length = chunk_length
        assert model_name in ['silero', 'pyannote']
        assert chunk_length > 0
        
        if model_name is 'silero':
            self.vad_pipeline, vad_utils = torch.hub.load(repo_or_dir = 'snakers4/silero-vad',
                                                          model = 'silero_vad',
                                                          force_reload = True,
                                                          onnx = False,
                                                          trust_repo = True,)
            (self.get_speech_timestamps, _, self.read_audio, _, _) = vad_utils
        else:
            if hf_token is None:
                print(
                    "Warning, no --hf_token used, needs to be saved in environment variable, otherwise will throw "
                    "error loading VAD model...")
            from pyannote.audio import Inference
            self.vad_pipeline = Inference(
                    "pyannote/segmentation",
                    pre_aggregation_hook = lambda segmentation: segmentation,
                    use_auth_token = hf_token,
                    device = torch.device(device),
            )
        pass
    
    def get_segments_pyannote(self, audio_path: str, chunk_size: int = 0):
        """use pyannote to get segments of speech"""
        segments = self.vad_pipeline(audio_path)
        if not chunk_size:
            chunk_size = self.chunk_length
        
        assert chunk_size > 0
        binarize = Binarize(max_duration=chunk_size)
        segments = binarize(segments)
        segments_list = []
        for speech_turn in segments.get_timeline():
            segments_list.append(Segment(speech_turn.start, speech_turn.end, "UNKNOWN"))
        
        return segments_list
    
    def get_segments_silero_vad(self, audio_path: str = 'audio.wav', sample_rate: int = 0):
        """use silero to get segments of speech"""""
        # If audio sample rate is not provided, we read it from the audio file ourselves
        if not sample_rate:
            print("Reading sample rate from audio file...")
            with wave.open(audio_path, 'rb') as f:
                sample_rate = f.getframerate()
        
        # https://github.com/snakers4/silero-vad/wiki/Examples-and-Dependencies
        timestamps = self.get_speech_timestamps(self.read_audio(audio_path, sampling_rate=sample_rate),
                                                model=self.vad_pipeline,
                                                sampling_rate=sample_rate)
        # sample output: [{'end': 664992, 'start': 181344}, {'end': 1373088, 'start': 672864}] when sample_rate=44100
        # Segment defined in pyannote Segment is in seconds
        return [Segment(i['start']/sample_rate, i['end']/sample_rate, "UNKNOWN") for i in timestamps]
    
    def get_segments(self, audio_path: str, sample_rate: int = 0) -> List[dict]:
        """
        Get segments of speech from audio file by model.
        
        Return: List of segments, each segment is a dict with keys "start", "end", "segments".
        """
        if self.model_name is 'silero':
            vad_segments = self.get_segments_silero_vad(audio_path, sample_rate)
        else:
            vad_segments = self.get_segments_pyannote(audio_path, self.chunk_length)
        
        return self.merge_chunks(vad_segments, self.chunk_length)
        
    @staticmethod
    def merge_chunks(segments_list, chunk_size=30):
        """
        Merge VAD segments into larger segments of approximately size ~CHUNK_LENGTH.
        TODO: Make sure VAD segment isn't too long, otherwise it will cause OOM when input to alignment model
        TODO: Or sliding window alignment model over long segment.
        """
        curr_end = 0
        merged_segments = []
        seg_idxs = []
        speaker_idxs = []
    
        assert chunk_size > 0
    
        assert segments_list, "segments_list is empty."
        # Make sure the starting point is the start of the segment.
        curr_start = segments_list[0].start
    
        for seg in segments_list:
            if seg.end - curr_start > chunk_size and curr_end-curr_start > 0:
                merged_segments.append({
                    "start": curr_start,
                    "end": curr_end,
                    "segments": seg_idxs,
                })
                curr_start = seg.start
                seg_idxs = []
                speaker_idxs = []
            curr_end = seg.end
            seg_idxs.append((seg.start, seg.end))
            speaker_idxs.append(seg.speaker)
        # add final
        merged_segments.append({
                    "start": curr_start,
                    "end": curr_end,
                    "segments": seg_idxs,
                })
        return merged_segments

class Binarize:
    """Binarize detection scores using hysteresis thresholding
    Parameters
    ----------
    onset : float, optional
        Onset threshold. Defaults to 0.5.
    offset : float, optional
        Offset threshold. Defaults to `onset`.
    min_duration_on : float, optional
        Remove active regions shorter than that many seconds. Defaults to 0s.
    min_duration_off : float, optional
        Fill inactive regions shorter than that many seconds. Defaults to 0s.
    pad_onset : float, optional
        Extend active regions by moving their start time by that many seconds.
        Defaults to 0s.
    pad_offset : float, optional
        Extend active regions by moving their end time by that many seconds.
        Defaults to 0s.
    max_duration: float
        The maximum length of an active segment, divides segment at timestamp with lowest score.
    Reference
    ---------
    Gregory Gelly and Jean-Luc Gauvain. "Minimum Word Error Training of
    RNN-based Voice Activity Detection", InterSpeech 2015.

    Pyannote-audio
    """
    
    def __init__(
            self,
            onset: float = 0.5,
            offset: Optional[float] = None,
            min_duration_on: float = 0.0,
            min_duration_off: float = 0.0,
            pad_onset: float = 0.0,
            pad_offset: float = 0.0,
            max_duration: float = float('inf')
    ):
        
        super().__init__()
        
        self.onset = onset
        self.offset = offset or onset
        
        self.pad_onset = pad_onset
        self.pad_offset = pad_offset
        
        self.min_duration_on = min_duration_on
        self.min_duration_off = min_duration_off
        
        self.max_duration = max_duration
    
    def __call__(self, scores: SlidingWindowFeature) -> Annotation:
        """Binarize detection scores
        Parameters
        ----------
        scores : SlidingWindowFeature
            Detection scores.
        Returns
        -------
        active : Annotation
            Binarized scores.
        """
        
        num_frames, num_classes = scores.data.shape
        frames = scores.sliding_window
        timestamps = [frames[i].middle for i in range(num_frames)]
        
        # annotation meant to store 'active' regions
        active = Annotation()
        for k, k_scores in enumerate(scores.data.T):
            
            label = k if scores.labels is None else scores.labels[k]
            
            # initial state
            start = timestamps[0]
            is_active = k_scores[0] > self.onset
            curr_scores = [k_scores[0]]
            curr_timestamps = [start]
            for t, y in zip(timestamps[1:], k_scores[1:]):
                # currently active
                if is_active:
                    curr_duration = t - start
                    if curr_duration > self.max_duration:
                        # if curr_duration > 15:
                        # import pdb; pdb.set_trace()
                        search_after = len(curr_scores) // 2
                        # divide segment
                        min_score_div_idx = search_after + np.argmin(curr_scores[search_after:])
                        min_score_t = curr_timestamps[min_score_div_idx]
                        region = Segment(start - self.pad_onset, min_score_t + self.pad_offset)
                        active[region, k] = label
                        start = curr_timestamps[min_score_div_idx]
                        curr_scores = curr_scores[min_score_div_idx + 1:]
                        curr_timestamps = curr_timestamps[min_score_div_idx + 1:]
                    # switching from active to inactive
                    elif y < self.offset:
                        region = Segment(start - self.pad_onset, t + self.pad_offset)
                        active[region, k] = label
                        start = t
                        is_active = False
                        curr_scores = []
                        curr_timestamps = []
                # currently inactive
                else:
                    # switching from inactive to active
                    if y > self.onset:
                        start = t
                        is_active = True
                curr_scores.append(y)
                curr_timestamps.append(t)
            
            # if active at the end, add final region
            if is_active:
                region = Segment(start - self.pad_onset, t + self.pad_offset)
                active[region, k] = label
        
        # because of padding, some active regions might be overlapping: merge them.
        # also: fill same speaker gaps shorter than min_duration_off
        if self.pad_offset > 0.0 or self.pad_onset > 0.0 or self.min_duration_off > 0.0:
            if self.max_duration < float("inf"):
                raise NotImplementedError(f"This would break current max_duration param")
            active = active.support(collar = self.min_duration_off)
        
        # remove tracks shorter than min_duration_on
        if self.min_duration_on > 0:
            for segment, track in list(active.itertracks()):
                if segment.duration < self.min_duration_on:
                    del active[segment, track]
        
        return active


def merge_vad(vad_arr, pad_onset = 0.0, pad_offset = 0.0, min_duration_off = 0.0, min_duration_on = 0.0):
    
    active = Annotation()
    for k, vad_t in enumerate(vad_arr):
        region = Segment(vad_t[0] - pad_onset, vad_t[1] + pad_offset)
        active[region, k] = 1
    
    if pad_offset > 0.0 or pad_onset > 0.0 or min_duration_off > 0.0:
        active = active.support(collar = min_duration_off)
    
    # remove tracks shorter than min_duration_on
    if min_duration_on > 0:
        for segment, track in list(active.itertracks()):
            if segment.duration < min_duration_on:
                del active[segment, track]
    
    active = active.for_json()
    active_segs = pd.DataFrame([x['segment'] for x in active['content']])
    return active_segs


if __name__ == "__main__":
    # from pyannote.audio import Inference
    # hook = lambda segmentation: segmentation
    # inference = Inference("pyannote/segmentation", pre_aggregation_hook=hook)
    # audio = "/tmp/11962.wav" 
    # scores = inference(audio)
    # binarize = Binarize(max_duration=15)
    # anno = binarize(scores)
    # res = []
    # for ann in anno.get_timeline():
    #     res.append((ann.start, ann.end))
    
    # res = pd.DataFrame(res)
    # res[2] = res[1] - res[0]
    import pandas as pd
    
    input_fp = "tt298650_sync.wav"
    df = pd.read_csv(f"/work/maxbain/tmp/{input_fp}.sad", sep = " ", header = None)
    print(len(df))
    N = 0.15
    g = df[0].sub(df[1].shift())
    input_base = input_fp.split('.')[0]
    df = df.groupby(g.gt(N).cumsum()).agg({0: 'min', 1: 'max'})
    df.to_csv(f"/work/maxbain/tmp/{input_base}.lab", header = None, index = False, sep = " ")
    print(df)
    import pdb ;
    
    pdb.set_trace()
