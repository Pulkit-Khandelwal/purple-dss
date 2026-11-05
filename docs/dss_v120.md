# Purple MRI — ITK-SNAP DSS 0.3.0

This service runs the adult ex vivo pipeline in Purple MRI Docker 1.2.0.
Each ticket processes one hemisphere and one MRI contrast.

## Submit from ITK-SNAP

1. Open your hemisphere MRI as the main image.
2. Open **Tools → Distributed Segmentation Service** and connect to
   `https://dss.itksnap.org` using your DSS account.
3. Select **purple 0.3.0**.
4. Assign the main image to exactly one tag from the table below.
5. Submit. Results are delivered as six attachments on progress messages.
   Use each message's paperclip to download its archive.

| MRI contrast | Left hemisphere | Right hemisphere |
|---|---|---|
| T2w | `InputMRI_T2W_LH` | `InputMRI_T2W_RH` |
| CISS | `InputMRI_CISS_LH` | `InputMRI_CISS_RH` |
| FLASH | `InputMRI_FLASH_LH` | `InputMRI_FLASH_RH` |
| T2* | `InputMRI_T2STAR_LH` | `InputMRI_T2STAR_RH` |

The provider requires exactly one selection. The selected contrast and hemisphere
are passed explicitly to the container. T2w/CISS use the baseline tissue workflow;
FLASH/T2* additionally use Task278 and its GM cleanup. The MTL rim correction
operates on the detailed tissue merge for all four contrasts. Reconstruction uses
`12_surface_labels`. Brainnetome correction uses the selected hemisphere, and the
final atlas merge reuses the completed surface projections.

## Six downloads

Extract all six archives into the same directory. Load a NIfTI segmentation as an
overlay on the original MRI. The first segmentation archive is delivered after DL;
the other archives follow reconstruction and atlas merging.

| Archive | Contents |
|---|---|
| `purple_segmentations.tar.gz` | Raw baseline prediction, corrected baseline, MTL, subcortical, detailed final merge and reconstruction labels; raw and cleaned Task278 for FLASH/T2* |
| `purple_parcellations_01_classic.zip` | Final DKT and Destrieux merges with lookup tables; existing Desikan–Killiany, DKT and Destrieux surface projections |
| `purple_parcellations_02_modern.zip` | Final Glasser, Schaefer400 and Brainnetome merges with lookup tables and their surface projections |
| `purple_parcellations_03_additional.zip` | Final Economo and Julich merges with lookup tables; Economo, Julich and white-matter surface parcellations |
| `purple_surfaces.tar.gz` | Selected-hemisphere surfaces, vertex metrics, annotations, labels and statistics |
| `purple_reports.tar.gz` | Validation, refinement, atlas and surface reports, timings, logs and file manifest |

Within atlas ZIPs, `CASE/final_merged/` contains the seven final atlas-plus-DL
segmentations; `CASE/surface_projection/` contains the nine FreeSurfer parcellation
volumes. `dl/11_final_merged` retains generic cortical GM101, while the seven final
atlas volumes assign cortical parcel labels to that GM. `dl/12_surface_labels`
uses the coarse 0–10 reconstruction convention.

## Provider setup on CLAM

Required: Docker with NVIDIA support, `itksnap-wt` authenticated for provider
`purple`, Python 3.8+, and a FreeSurfer license. The wrapper activates
`/data/pulkit/pulks_env` when present.

The provider uses the published Docker digest:

```
pulks/docker_purple_mri@sha256:c8df848d4649c1c0a8f8a7ccf7911ac5bd5df86442ff0e66eaf5f3dd2ce9558d
```

Install from this package and test packaging on the completed 122104 FLASH run:

```bash
bash install_on_clam.sh
bash test_packaging_on_clam.sh
```

The installer snapshots the replaced files outside the Git repository. It checks
the existing provider/descriptor hashes against the CLAM inventory before writing.
The packaging test validates existing outputs, converts the nine existing MGZ
parcellations to NIfTI, and creates six verified archives in a fresh directory.

Publish the release files and register the exact Git commit with DSS:

```bash
bash publish_on_clam.sh
```

DSS registration uses `-dssa-providers-services-add` and requires an account with
the appropriate server privileges. Confirm `purple 0.3.0` in
`itksnap-wt -P -dssp-services-list`, then start the worker in a persistent terminal:

```bash
cd /data/pulkit/purple-dss
GPU=0 THREADS=8 bash daemon/run_purple_dss_provider.sh run
```

Use `run --once` to process at most one claimed ticket. Set `GPU` to an available
device. Set `FS_LICENSE_HOST` to override the license file. New ticket attempts
are stored under `/data/pulkit/purple-dss/work/v120/`.

## Delivery recovery

All six archives must pass payload checks and upload successfully before the
provider marks a ticket successful. Upload receipts are keyed by ticket ID and
archive hash. After an interrupted delivery, retry the existing validated package:

```bash
bash daemon/run_purple_dss_provider.sh upload-only TICKET_ID /path/to/downloads --mark-success
```

Local receipts avoid repeating confirmed uploads. A timeout before confirmation
can still leave a duplicate server attachment. If a claim request times out,
inspect DSS ticket state before restarting the provider.

## Local tests

```bash
python3 -m unittest discover -s tests -v
```

The local suite tests routing, packaging and failure handling with mocked external
commands. The CLAM packaging test exercises the real container and existing data;
a submitted DSS ticket exercises the registered service and attachment delivery.

Provider API reference:
https://alfabis-server.readthedocs.io/en/latest/service_quick_start.html
