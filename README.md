# vidkit

Inspect video metadata and re-encode to efficient HEVC on Apple Silicon.

Requires `ffmpeg` and `ffprobe` on PATH.

## Usage

```sh
# inspect metadata
./vidkit.py info video.mp4

# re-encode (default: hardware HEVC)
./vidkit.py convert video.mp4
./vidkit.py convert folder/         # batch -> folder/hevc/
./vidkit.py convert folder/ --dry-run
```

## Encoders (`--encoder`)

| option | backend | notes |
|---|---|---|
| `media` | hevc_videotoolbox | default, hardware, fast |
| `cpu` | libx265 | software, best quality-per-bit |
| `av1` | libsvtav1 | software, best quality |
| `h264` | h264_videotoolbox | hardware, max compatibility |

`gpu` / `hw` are aliases for `media`.

## Options

- `--quality N` — VideoToolbox quality 1-100 (default 60)
- `--crf N` — libx265 CRF (default 24)
- `--av1-crf N` / `--av1-preset N` — AV1 tuning
- `--audio-bitrate N` — re-encode audio to AAC at N kbit/s
- `--ext mp4|mov` — output container (default mp4)
- `--force` — overwrite existing outputs
- `--out-dir DIR` — custom output directory

## License

MIT
