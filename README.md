# purple-dss

**ITK-SNAP Distributed Segmentation Service (DSS) interface for [purple-mri](https://github.com/Pulkit-Khandelwal/purple-mri)**

`purple-dss` connects the [purple-mri](https://purple-mri.readthedocs.io/en/latest/) postmortem brain MRI processing framework to [ITK-SNAP's Distributed Segmentation Service](https://dss.itksnap.org/). It is designed to let researchers submit an ex vivo brain MRI from ITK-SNAP, process it on a remote GPU-enabled provider, and retrieve the resulting segmentations and associated surface outputs.

> **Status: experimental.** The combined Docker backend and DSS result-delivery workflow are undergoing end-to-end testing. Do not assume a submitted ticket will complete successfully or that every optional output is available.

## Processing workflow

```text
ITK-SNAP (input MRI)
        |
        v
DSS server (ticket submission and tracking)
        |
        v
purple provider (remote processing)
        |
        v
purple-mri Docker: segmentation -> topology correction -> merging
                                  -> surface reconstruction -> parcellation
        |
        v
DSS results: ITK-SNAP workspace + optional file attachments
```

The current backend is based on `pulks/docker_purple_mri:v1.1.0`, which supports the combined `exvivo_dl exvivo_surface` workflow. The Docker pipeline includes adult ex vivo deep-learning segmentation, post-hoc topology correction, merged anatomical segmentations, cortical surface reconstruction, and atlas-based parcellation. See the [Docker user guide](https://github.com/Pulkit-Khandelwal/purple-mri/blob/main/docker/exvivo_docker.md) for input preparation, Docker requirements, and detailed output descriptions.

## Submit an MRI from ITK-SNAP

1. Open an appropriately preprocessed **adult ex vivo hemisphere MRI** in ITK-SNAP.
2. Select **Tools -> Distributed Segmentation Service**.
3. Connect to `https://dss.itksnap.org` and authenticate if prompted.
4. On the **Submit** tab, select the `purple` service available to your account.
5. Assign the input image using the tag expected by the **registered service version** (see below).
6. Submit the ticket, then follow its status in the **Results** tab.
7. If processing succeeds, download the returned workspace. ITK-SNAP will open the downloaded workspace with its result layers.

**Service versions and hemisphere selection**

| Version | Status | MRI input tag | Hemisphere |
| --- | --- | --- | --- |
| `0.1.0` | Existing DSS registration; RH-only backend test | `InputMRI` | Right hemisphere (`rh`) |
| `0.2.0` | Descriptor published in this repository; **not yet registered on DSS** | `InputMRI_LH` or `InputMRI_RH` | Intended LH/RH selection; pending activation and validation |

**Use the tag shown by the DSS Submit interface, not just the version in this GitHub repository.** The repository's current `service.json` describes the proposed `0.2.0` interface, but publishing the JSON does not automatically update the DSS server registration.

## What the user receives

Two kinds of outputs are supported by the provider:

| Delivery | Contents | How to access |
| --- | --- | --- |
| **ITK-SNAP result workspace** (`.itksnap`) | Original MRI plus available segmentation layers, such as the corrected Purple segmentation and final merged segmentation; a volumetric atlas parcellation may be included when produced successfully | Download the successful ticket from **Results**; the workspace opens in ITK-SNAP |
| **Optional attachments** | A compressed `.tar.gz` archive of selected FreeSurfer-style surfaces (`rh.white`, `rh.pial`, `rh.thickness`, `rh.curv`, `rh.sulc` for an RH run), MRI/label/statistics files, logs, and timing or QC reports | Open or download the attachment separately from the ticket |

The `.itksnap` workspace collects the image layers together; users do **not** have to download each segmentation individually. Surface files are provided as a separate compressed attachment rather than automatically displayed as meshes inside ITK-SNAP. Optional attachments can be unavailable if their upload fails.

## Repository layout

```text
purple-dss/
├── service.json          # Public service description
└── desc/
    └── service.json      # DSS service descriptor
```

This repository defines the DSS-facing service interface. The image-processing implementation is part of [purple-mri](https://github.com/Pulkit-Khandelwal/purple-mri), and execution requires a separately running DSS provider connected to the DSS server. A public service description by itself does not run inference.

## Requirements and limitations

- ITK-SNAP with DSS support, a working DSS connection, and an available `purple` provider.
- A supported, preprocessed adult ex vivo MRI; process one hemisphere per request.
- The remote backend requires an NVIDIA GPU for DL inference and a FreeSurfer license for surface processing.
- This is a research workflow, **not a clinical diagnostic service**. Check anatomical quality and parcellation results before analysis.
- Submit human MRI data only where institutional data-sharing and research approvals permit upload to the DSS server/provider.

## Related resources

- [purple-mri source](https://github.com/Pulkit-Khandelwal/purple-mri)
- [purple-mri documentation](https://purple-mri.readthedocs.io/en/latest/)
- [Docker workflow and outputs](https://github.com/Pulkit-Khandelwal/purple-mri/blob/main/docker/exvivo_docker.md)
- [Docker source/build instructions](https://github.com/Pulkit-Khandelwal/purple-mri/blob/main/docker/docker_files/README.md)
- [ITK-SNAP DSS user guide](https://alfabis-server.readthedocs.io/en/latest/user_quick_start.html)

**Maintainer:** [Pulkit Khandelwal](https://github.com/Pulkit-Khandelwal)
