# Daheng Imaging Dual Camera Trigger System

Developed for ArMMs

Used for synchronous camera triggering in fixed intervals

This console tool triggers two Daheng Imaging cameras at the same time using software triggers. You can take single shots or run a timed sequence, and every image is saved as an uncompressed TIFF.

## Requirements

- Two Daheng Imaging (Galaxy) cameras connected to the computer
- The [Daheng Galaxy SDK / camera driver](https://www.daheng-imaging.com/) for your operating system. The bundled `gxipy` package needs its runtime libraries.
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda

## Installation

1. Clone the repository:

   ```bash
   git clone <repo-url>
   cd daheng_imaging_dual_cam_trigger
   ```

2. Create and activate the conda environment (Python 3.9, numpy, pillow, termcolor):

   ```bash
   conda env create -f env.yml
   conda activate daheng
   ```

3. Check that both cameras show up in Daheng's Galaxy Viewer. Then close Galaxy Viewer, because the cameras can only be opened by one program at a time.

## Usage

Run from the project root:

```bash
python main.py
```

On startup the program:

1. Finds the connected cameras and opens the first two. Fewer than two cameras is an error.
2. Sets both cameras to software trigger mode.
3. Creates a session folder under `out/` and shows a `>` prompt.

### Commands

| Command | Action |
|---------|--------|
| `Enter` | Trigger both cameras once. The images are saved to `singles/`. |
| `l`     | Start a looped acquisition (see below). |
| `q`     | Quit. Images still waiting to be written are saved before the cameras close. |

### Looped acquisition

After typing `l`, you are asked for two values. Press Enter to accept the default shown in brackets.

```
> l
  Interval between acquisitions (ms) [1000]: 500
  Number of acquisitions [10]: 20
```

- Both cameras are triggered `count` times, `interval` ms apart.
- The loop runs in the background. You can't start a second loop until the current one finishes.
- Each loop is saved in its own numbered folder (`loop_1`, `loop_2`, ...) inside the current session.
- Pressing `q` (or `Ctrl+C`) stops a running loop.

## Output

```
out/
└── acquisition_YYYYMMDD_HHMM/
    ├── singles/
    │   ├── cam_1/cam_1_<timestamp_ns>.tiff
    │   └── cam_2/cam_2_<timestamp_ns>.tiff
    └── loops/
        ├── cam_1/loop_1/cam_1_<timestamp_ns>.tiff
        └── cam_2/loop_1/cam_2_<timestamp_ns>.tiff
```

- `<timestamp_ns>` is the capture time in nanoseconds since the Unix epoch, so sorting by file name sorts by capture time.
- `cam_1` and `cam_2` are the first and second cameras in the order the SDK finds them.
- Session folders are named down to the minute, so two runs started in the same minute write into the same folder.
- `out/` is created in the folder you run the program from.

## Troubleshooting

- **"Found only N camera(s). Two cameras are required."** Check the cables and power, and close Galaxy Viewer or any other program using the cameras.
- **"Failed to catch triggered image."** The camera didn't deliver a frame within 10 s. Check the connection and that the exposure time is shorter than the trigger interval.
- **gxipy fails to import or load its library.** Install the Daheng Galaxy SDK/driver and make sure the `daheng` environment is active.

## License

[MIT](LICENSE)
