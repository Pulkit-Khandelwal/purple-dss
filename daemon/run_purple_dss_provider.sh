#!/usr/bin/env bash
set -eu

###############################################################################
# purple-mri ITK-SNAP DSS provider (combined Docker image)
#
# Service/tags: purple / InputMRI_LH or InputMRI_RH
# Image:       pulks/docker_purple_mri:v1.1.0
# Pipeline:    exvivo_dl exvivo_surface (inside ONE container)
# Hemisphere:  selected per ticket from assigned input image tag
#
# Requirements on provider host: Docker with NVIDIA GPU access, itksnap-wt,
# a valid FreeSurfer license, GNU timeout, rsync, tar, and basic coreutils.
# FreeSurfer, atlases, custom pial, and the surface helpers live IN THE IMAGE.
#
# For each ticket, outputs are kept under:
#   /data/pulkit/purple-dss/work/ticket_<ID>/
#
# Non-critical DSS progress/log/attachment calls have bounded timeouts.
# Result upload and ticket-success operations remain mandatory.
###############################################################################

export DSS_SERVER="${DSS_SERVER:-https://dss.itksnap.org}"
DSS_PROVIDER="${DSS_PROVIDER:-purple}"
DSS_PROVIDER_CODE="${DSS_PROVIDER_CODE:-clam_01}"
DSS_SERVICE_NAME="${DSS_SERVICE_NAME:-purple}"

PURPLE_DSS_ROOT="${PURPLE_DSS_ROOT:-/data/pulkit/purple-dss}"
WORK_ROOT="${PURPLE_DSS_ROOT}/work"
PURPLE_DOCKER_IMAGE="${PURPLE_DOCKER_IMAGE:-pulks/docker_purple_mri:v1.1.0}"
DSS_SERVICE_VERSION="${DSS_SERVICE_VERSION:-0.2.0}"
THREADS="${THREADS:-20}"

DSS_META_TIMEOUT_SEC="${DSS_META_TIMEOUT_SEC:-30}"
DSS_ATTACH_TIMEOUT_SEC="${DSS_ATTACH_TIMEOUT_SEC:-240}"
DSS_CRITICAL_TIMEOUT_SEC="${DSS_CRITICAL_TIMEOUT_SEC:-300}"
CLAIM_SLEEP_SEC="${CLAIM_SLEEP_SEC:-10}"
ERROR_SLEEP_SEC="${ERROR_SLEEP_SEC:-20}"

# Optional: activating this existing host environment can expose itksnap-wt.
PY_ENV="${PY_ENV:-/data/pulkit/pulks_env/bin/activate}"
if [[ -f "${PY_ENV}" ]]; then
  # shellcheck disable=SC1090
  source "${PY_ENV}"
fi

fatal() { echo "ERROR: $*" >&2; exit 1; }

# Use a short, bounded request for DSS metadata operations. A temporary DSS
# outage should not kill a long-running DL/surface processing job.
safe_dss_meta() {
  if ! timeout -k 5s "${DSS_META_TIMEOUT_SEC}s" itksnap-wt "$@"; then
    echo "WARN: optional DSS metadata request failed/timed out: $*" >&2
  fi
  return 0
}

safe_progress() {
  local ticket="$1" value="$2"
  safe_dss_meta -dssp-tickets-set-progress "${ticket}" 0 1 "${value}"
}

safe_log() {
  local ticket="$1" message="$2"
  safe_dss_meta -dssp-tickets-log "${ticket}" info "${message}"
}

safe_attach() {
  local ticket="$1" name="$2" filename="$3" mimetype="$4"
  if [[ ! -s "${filename}" ]]; then
    echo "WARN: not attaching missing/empty file: ${filename}" >&2
    return 0
  fi
  if ! timeout -k 5s "${DSS_ATTACH_TIMEOUT_SEC}s" \
    itksnap-wt -dssp-tickets-attach "${ticket}" "${name}" "${filename}" "${mimetype}"; then
    echo "WARN: DSS attachment failed/timed out; local copy preserved: ${filename}" >&2
  fi
  return 0
}

# Critical DSS operations: retry transient failures, but do not claim success
# unless the command itself actually returns success.
critical_dss() {
  local attempt=1 max_attempts=2
  while (( attempt <= max_attempts )); do
    if timeout -k 5s "${DSS_CRITICAL_TIMEOUT_SEC}s" itksnap-wt "$@"; then return 0; fi
    echo "ERROR: DSS operation failed (attempt ${attempt}/${max_attempts}): $*" >&2
    (( attempt == max_attempts )) && break
    sleep 10
    attempt=$((attempt + 1))
  done
  return 1
}

fail_ticket() {
  local ticket="$1" message="$2"
  echo "ERROR: ticket ${ticket}: ${message}" >&2
  if ! timeout -k 5s 60s itksnap-wt -dssp-tickets-fail "${ticket}" "${message}"; then
    echo "WARN: could not report failure to DSS for ticket ${ticket}" >&2
  fi
}

# Find the license without printing its contents. Override this auto-detection
# with: export FS_LICENSE_HOST=/path/to/license.txt
FS_LICENSE_HOST="${FS_LICENSE_HOST:-}"
if [[ -z "${FS_LICENSE_HOST}" ]]; then
  for candidate in \
    /data/pulkit/exvivo_reg_reconstruct/freesurfer_installation/freesurfer/license.txt \
    /data/pulkit/exvivo_reg_reconstruct/freesurfer_installation/freesurfer/.license \
    "${HOME}/.freesurfer/license.txt" \
    "${HOME}/.freesurfer/.license" \
    "${HOME}/license.txt"; do
    if [[ -s "${candidate}" ]]; then
      FS_LICENSE_HOST="${candidate}"
      break
    fi
  done
fi

###############################################################################
# Startup checks
###############################################################################

for cmd in itksnap-wt docker timeout rsync tar awk grep sed cp; do
  command -v "${cmd}" >/dev/null 2>&1 || fatal "missing command: ${cmd}"
done
[[ "${THREADS}" =~ ^[1-9][0-9]*$ ]] || fatal "THREADS must be a positive integer"
[[ -n "${FS_LICENSE_HOST}" && -s "${FS_LICENSE_HOST}" ]] || \
  fatal "FreeSurfer license not found. Set FS_LICENSE_HOST=/absolute/path/to/license.txt"
[[ "${FS_LICENSE_HOST}" == /* ]] || fatal "FS_LICENSE_HOST must be absolute"

if ! docker image inspect "${PURPLE_DOCKER_IMAGE}" >/dev/null 2>&1; then
  fatal "Docker image not present locally: ${PURPLE_DOCKER_IMAGE}. Run: docker pull ${PURPLE_DOCKER_IMAGE}"
fi

mkdir -p "${WORK_ROOT}"

echo "============================================================"
echo "purple DSS provider -- Docker DL + surfaces"
echo "DSS server:      ${DSS_SERVER}"
echo "Service:         ${DSS_SERVICE_NAME} ${DSS_SERVICE_VERSION}"
echo "Provider:        ${DSS_PROVIDER} (${DSS_PROVIDER_CODE})"
echo "itksnap-wt:      $(command -v itksnap-wt)"
echo "Docker image:    ${PURPLE_DOCKER_IMAGE}"
echo "Stages:          exvivo_dl exvivo_surface"
echo "Hemisphere:      selected per ticket from InputMRI_LH or InputMRI_RH"
echo "Surface threads: ${THREADS}"
echo "License file:    ${FS_LICENSE_HOST} (read-only mount)"
echo "Work root:       ${WORK_ROOT}"
echo "============================================================"

###############################################################################
# Claim and process one ticket at a time
###############################################################################

while true; do
  echo
  echo "Looking up purple DSS service hash..."
  SERVICES_FILE="${WORK_ROOT}/services_list.txt"

  if ! timeout -k 5s 60s itksnap-wt -P -dssp-services-list > "${SERVICES_FILE}" 2>&1; then
    echo "WARN: DSS service listing failed. Retrying..." >&2
    sleep "${ERROR_SLEEP_SEC}"
    continue
  fi
  GITHASH=$(awk -v svc="${DSS_SERVICE_NAME}" -v ver="${DSS_SERVICE_VERSION}" \
    '$1 == svc && $2 == ver {print $3; exit}' "${SERVICES_FILE}")
  if [[ -z "${GITHASH}" ]]; then
    echo "WARN: service ${DSS_SERVICE_NAME} version ${DSS_SERVICE_VERSION} not found in DSS listing." >&2
    sleep "${ERROR_SLEEP_SEC}"
    continue
  fi
  echo "Service hash: ${GITHASH}"

  CLAIM_FILE="${WORK_ROOT}/claim.txt"
  if ! timeout -k 5s 60s itksnap-wt -dssp-services-claim \
    "${GITHASH}" "${DSS_PROVIDER}" "${DSS_PROVIDER_CODE}" \
    > "${CLAIM_FILE}" 2>&1; then
    echo "No ticket claimed (or transient DSS error)."
    sleep "${CLAIM_SLEEP_SEC}"
    continue
  fi

  cat "${CLAIM_FILE}"
  TICKET_ID=$(awk '/^1>/ {print $2; exit}' "${CLAIM_FILE}")
  if [[ ! "${TICKET_ID}" =~ ^[0-9]+$ ]]; then
    echo "WARN: could not parse numeric ticket ID." >&2
    sleep "${CLAIM_SLEEP_SEC}"
    continue
  fi

  echo "Claimed ticket: ${TICKET_ID}"
  WORKDIR="${WORK_ROOT}/ticket_${TICKET_ID}"
  if [[ -e "${WORKDIR}" ]]; then
    mv -- "${WORKDIR}" "${WORKDIR}.previous.$(date +%Y%m%d_%H%M%S)"
  fi
  mkdir -p "${WORKDIR}"

  DOWNLOAD_FILE="${WORKDIR}/download.txt"
  echo "Downloading ticket ${TICKET_ID}..."
  if ! critical_dss -dssp-tickets-download "${TICKET_ID}" "${WORKDIR}" \
      > "${DOWNLOAD_FILE}" 2>&1; then
    cat "${DOWNLOAD_FILE}" >&2 || true
    fail_ticket "${TICKET_ID}" "purple: failed to download input workspace"
    continue
  fi
  cat "${DOWNLOAD_FILE}"

  INPUT_WSP=$(awk '/^1>.*\.itksnap$/ {sub(/^1> /, ""); print; exit}' "${DOWNLOAD_FILE}")
  if [[ -z "${INPUT_WSP}" || ! -f "${INPUT_WSP}" ]]; then
    fail_ticket "${TICKET_ID}" "purple: downloaded ITK-SNAP workspace not found"
    continue
  fi
  echo "Input workspace: ${INPUT_WSP}"
  safe_log "${TICKET_ID}" "purple: downloaded input workspace"
  safe_progress "${TICKET_ID}" 0.03

  # The DSS GUI does not implement a free-form Hemisphere parameter.
  # Instead, the user assigns exactly one of these optional MainImage tags.
  # A missing tag may cause -llf to return nonzero; do not fail on lookup alone.
  INPUT_MRI_LH=$(itksnap-wt -P -i "${INPUT_WSP}" -llf InputMRI_LH 2>/dev/null || true)
  INPUT_MRI_RH=$(itksnap-wt -P -i "${INPUT_WSP}" -llf InputMRI_RH 2>/dev/null || true)

  HAS_LH=0
  HAS_RH=0
  [[ -n "${INPUT_MRI_LH}" && -f "${INPUT_MRI_LH}" ]] && HAS_LH=1
  [[ -n "${INPUT_MRI_RH}" && -f "${INPUT_MRI_RH}" ]] && HAS_RH=1

  if [[ "${HAS_LH}" -eq 1 && "${HAS_RH}" -eq 0 ]]; then
    HEMI="lh"
    INPUT_MRI="${INPUT_MRI_LH}"
  elif [[ "${HAS_RH}" -eq 1 && "${HAS_LH}" -eq 0 ]]; then
    HEMI="rh"
    INPUT_MRI="${INPUT_MRI_RH}"
  else
    echo "ERROR: Input tags must identify exactly one hemisphere." >&2
    itksnap-wt -i "${INPUT_WSP}" -layers-list || true
    fail_ticket "${TICKET_ID}" \
      "purple: assign the main MRI to exactly one tag: InputMRI_LH or InputMRI_RH"
    continue
  fi

  echo "Input MRI: ${INPUT_MRI}"
  echo "Selected hemisphere: ${HEMI}"
  safe_log "${TICKET_ID}" "purple: user selected hemisphere ${HEMI}"

  #############################################################################
  # The combined image expects a single-channel *_0000.nii.gz file.
  #############################################################################

  RUN_DIR="${WORKDIR}/purple_container"
  INF_DIR="${RUN_DIR}/data_for_inference"
  OUT_ROOT="${INF_DIR}/output_from_nnunet_inference"
  DL_OUT_DIR="${OUT_ROOT}/exvivo_dl"
  SURF_OUT_DIR="${OUT_ROOT}/exvivo_surface"
  TIMING_DIR="${OUT_ROOT}/timing"
  mkdir -p "${INF_DIR}"

  if ! cp -- "${INPUT_MRI}" "${INF_DIR}/case_0000.nii.gz"; then
    fail_ticket "${TICKET_ID}" "purple: failed to stage input MRI"
    continue
  fi

  COMBINED_LOG="${WORKDIR}/purple_combined_docker.log"
  echo "Running adult DL + topology correction + merge + surface reconstruction..."
  echo "Image: ${PURPLE_DOCKER_IMAGE}"
  echo "Combined log: ${COMBINED_LOG}"
  safe_log "${TICKET_ID}" "purple: running exvivo_dl and exvivo_surface in combined Docker image"
  safe_progress "${TICKET_ID}" 0.10

  # Intentionally no -it: this script runs unattended as a DSS daemon.
  # No host-side FreeSurfer, atlas copy, or separate surface scripts are needed.
  if ! docker run --rm --gpus all --shm-size=8g \
    -v "${RUN_DIR}:/data/exvivo" \
    -v "${FS_LICENSE_HOST}:/license.txt:ro" \
    -e "NO_FSFAST=1" \
    -e "EXVIVO_HEMI=${HEMI}" \
    -e "EXVIVO_THREADS=${THREADS}" \
    -e "FS_LICENSE=/license.txt" \
    -e "EXVIVO_TIMING_DIR=/data/exvivo/data_for_inference/output_from_nnunet_inference/timing" \
    "${PURPLE_DOCKER_IMAGE}" \
    /bin/bash /src/commands_nnunet_inference.sh exvivo_dl exvivo_surface \
    > "${COMBINED_LOG}" 2>&1; then
    echo "ERROR: combined pipeline failed. Log: ${COMBINED_LOG}" >&2
    tail -n 75 "${COMBINED_LOG}" >&2 || true
    safe_attach "${TICKET_ID}" "purple combined pipeline log" "${COMBINED_LOG}" text/plain
    fail_ticket "${TICKET_ID}" "purple: combined DL/surface pipeline failed; see log"
    continue
  fi
  echo "Combined pipeline succeeded."
  safe_progress "${TICKET_ID}" 0.83

  #############################################################################
  # Check exact new output locations (do not mistake intermediate label volumes
  # such as files_prepared_for_pipeline/case_label_1_cc.nii.gz for parcellations).
  #############################################################################

  ORIGINAL_SEG="${DL_OUT_DIR}/01_purple_original/case.nii.gz"
  CORRECTED_SEG="${DL_OUT_DIR}/05_purple_corrected/case.nii.gz"
  MERGED_SEG="${DL_OUT_DIR}/11_final_merged/case.nii.gz"
  CASE_OUT_DIR="${SURF_OUT_DIR}/case"

  if [[ ! -s "${ORIGINAL_SEG}" || ! -s "${CORRECTED_SEG}" || \
        ! -s "${MERGED_SEG}" || ! -f "${SURF_OUT_DIR}/COMPLETE" || \
        ! -d "${CASE_OUT_DIR}" ]]; then
    echo "ERROR: expected combined Docker outputs missing:" >&2
    for file in "${ORIGINAL_SEG}" "${CORRECTED_SEG}" "${MERGED_SEG}" "${SURF_OUT_DIR}/COMPLETE"; do
      [[ -e "${file}" ]] || echo "  MISSING: ${file}" >&2
    done
    [[ -d "${CASE_OUT_DIR}" ]] || echo "  MISSING: ${CASE_OUT_DIR}" >&2
    fail_ticket "${TICKET_ID}" "purple: missing required combined Docker outputs"
    continue
  fi

  missing_surface=0
  for surface in "${HEMI}.pial" "${HEMI}.white" "${HEMI}.thickness" "${HEMI}.curv" "${HEMI}.sulc"; do
    if [[ ! -s "${CASE_OUT_DIR}/surf/${surface}" ]]; then
      echo "ERROR: missing surface ${CASE_OUT_DIR}/surf/${surface}" >&2
      missing_surface=1
    fi
  done
  if (( missing_surface )); then
    fail_ticket "${TICKET_ID}" "purple: missing required surface files"
    continue
  fi

  echo "Original DL:   ${ORIGINAL_SEG}"
  echo "Corrected DL:  ${CORRECTED_SEG}"
  echo "Merged DL:     ${MERGED_SEG}"
  echo "Surface case:  ${CASE_OUT_DIR}"

  #############################################################################
  # Package minimal FreeSurfer subject, preserving only five requested surfaces.
  # MRI: exclude case.mgz and transforms/; keep label/ and stats/.
  #############################################################################

  TMP_PACK="${WORKDIR}/surface_package_minimal"
  SURFACE_TAR="${WORKDIR}/purple_surface_outputs.tar.gz"
  mkdir -p "${TMP_PACK}/case/surf" "${TMP_PACK}/case/mri" \
           "${TMP_PACK}/case/label" "${TMP_PACK}/case/stats"

  PACK_OK=1
  for surface in "${HEMI}.pial" "${HEMI}.white" "${HEMI}.thickness" \
                 "${HEMI}.curv" "${HEMI}.sulc"; do
    if ! cp -a -- "${CASE_OUT_DIR}/surf/${surface}" "${TMP_PACK}/case/surf/"; then
      PACK_OK=0
    fi
  done

  # No pial.T1.iter*, smoothwm*, or other surf intermediates are copied.
  if [[ -d "${CASE_OUT_DIR}/mri" ]]; then
    if ! rsync -a --exclude='transforms/' --exclude='case.mgz' \
      "${CASE_OUT_DIR}/mri/" "${TMP_PACK}/case/mri/"; then
      PACK_OK=0
    fi
  fi
  if [[ -d "${CASE_OUT_DIR}/label" ]]; then
    if ! rsync -a "${CASE_OUT_DIR}/label/" "${TMP_PACK}/case/label/"; then
      PACK_OK=0
    fi
  fi
  if [[ -d "${CASE_OUT_DIR}/stats" ]]; then
    if ! rsync -a "${CASE_OUT_DIR}/stats/" "${TMP_PACK}/case/stats/"; then
      PACK_OK=0
    fi
  fi

  if (( PACK_OK )) && tar -czf "${SURFACE_TAR}" -C "${TMP_PACK}" case; then
    ls -lh "${SURFACE_TAR}"
  else
    echo "WARN: could not package surface outputs; workspace upload will proceed." >&2
    rm -f -- "${SURFACE_TAR}"
  fi

  # Small QC bundle: DL cleanup reports, timing summaries, source provenance.
  QC_DIR="${WORKDIR}/purple_qc_package"
  QC_TAR="${WORKDIR}/purple_qc_timings.tar.gz"
  mkdir -p "${QC_DIR}"
  if [[ -d "${DL_OUT_DIR}/reports" ]]; then
    cp -a -- "${DL_OUT_DIR}/reports" "${QC_DIR}/dl_reports" || true
  fi
  if [[ -d "${TIMING_DIR}" ]]; then
    cp -a -- "${TIMING_DIR}" "${QC_DIR}/timings" || true
  fi
  for name in hemisphere.txt source_revision.txt; do
    if [[ -f "${SURF_OUT_DIR}/${name}" ]]; then
      cp -a -- "${SURF_OUT_DIR}/${name}" "${QC_DIR}/${name}" || true
    fi
  done
  if [[ -n "$(ls -A "${QC_DIR}")" ]]; then
    tar -czf "${QC_TAR}" -C "${QC_DIR}" . || rm -f -- "${QC_TAR}"
  fi

  #############################################################################
  # Create ITK-SNAP workspace with real DL results (not preparation intermediates).
  #############################################################################

  safe_log "${TICKET_ID}" "purple: preparing corrected and merged segmentation layers"
  safe_progress "${TICKET_ID}" 0.88

  RESULT_WSP="${WORKDIR}/purple_result_corrected.itksnap"
  if ! itksnap-wt -i "${INPUT_WSP}" \
    -layers-add-seg "${CORRECTED_SEG}" \
    -props-set-nickname "purple corrected (10 labels)" \
    -o "${RESULT_WSP}"; then
    fail_ticket "${TICKET_ID}" "purple: could not create result workspace with corrected segmentation"
    continue
  fi

  MERGED_WSP="${WORKDIR}/purple_result_merged.itksnap"
  if ! itksnap-wt -i "${RESULT_WSP}" \
    -layers-add-seg "${MERGED_SEG}" \
    -props-set-nickname "purple final merged DL" \
    -o "${MERGED_WSP}"; then
    fail_ticket "${TICKET_ID}" "purple: could not add final merged segmentation to workspace"
    continue
  fi
  RESULT_WSP="${MERGED_WSP}"

  # Optional REAL parcellation: FreeSurfer outputs DKTatlas.mgz. Convert it to
  # NIfTI inside the same image, then add it as an ITK-SNAP layer.
  DKT_MGZ="${CASE_OUT_DIR}/mri/DKTatlas.mgz"
  DKT_NII="${OUT_ROOT}/purple_DKTatlas.nii.gz"
  DKT_CONVERT_LOG="${WORKDIR}/purple_DKT_convert.log"
  if [[ -s "${DKT_MGZ}" ]]; then
    if docker run --rm \
      -v "${RUN_DIR}:/data/exvivo" \
      -v "${FS_LICENSE_HOST}:/license.txt:ro" \
      -e "FS_LICENSE=/license.txt" \
      "${PURPLE_DOCKER_IMAGE}" \
      /bin/bash -c 'source /opt/freesurfer/SetUpFreeSurfer.sh >/dev/null 2>&1 && mri_convert "$1" "$2"' \
      _ \
      /data/exvivo/data_for_inference/output_from_nnunet_inference/exvivo_surface/case/mri/DKTatlas.mgz \
      /data/exvivo/data_for_inference/output_from_nnunet_inference/purple_DKTatlas.nii.gz \
      > "${DKT_CONVERT_LOG}" 2>&1 && [[ -s "${DKT_NII}" ]]; then
      DKT_WSP="${WORKDIR}/purple_result_with_DKT.itksnap"
      if itksnap-wt -i "${RESULT_WSP}" \
        -layers-add-seg "${DKT_NII}" \
        -props-set-nickname "purple DKT surface parcellation" \
        -o "${DKT_WSP}"; then
        RESULT_WSP="${DKT_WSP}"
      else
        echo "WARN: DKT NIfTI conversion worked, but adding DKT layer failed." >&2
      fi
    else
      echo "WARN: optional DKTatlas.mgz -> NIfTI conversion failed; see ${DKT_CONVERT_LOG}" >&2
    fi
  else
    echo "No DKTatlas.mgz found; not adding DKT atlas as volume layer."
  fi

  echo "Result workspace: ${RESULT_WSP}"
  safe_log "${TICKET_ID}" "purple: uploading combined pipeline results"
  safe_progress "${TICKET_ID}" 0.93

  # Upload first so an optional large tar attachment cannot prevent delivery
  # of the actual ITK-SNAP results.
  if ! critical_dss -i "${RESULT_WSP}" -dssp-tickets-upload "${TICKET_ID}"; then
    fail_ticket "${TICKET_ID}" "purple: result workspace upload failed"
    continue
  fi

  # Attachments are best-effort. The local copies are always preserved.
  safe_attach "${TICKET_ID}" "purple minimal FreeSurfer case outputs" "${SURFACE_TAR}" application/gzip
  safe_attach "${TICKET_ID}" "purple QC and timing reports" "${QC_TAR}" application/gzip
  safe_attach "${TICKET_ID}" "purple combined Docker log" "${COMBINED_LOG}" text/plain
  safe_attach "${TICKET_ID}" "purple surface reconstruction log" \
    "${SURF_OUT_DIR}/surface_pipeline.log" text/plain

  safe_progress "${TICKET_ID}" 1
  if critical_dss -dssp-tickets-success "${TICKET_ID}"; then
    echo "Ticket ${TICKET_ID} completed successfully."
  else
    echo "ERROR: ticket ${TICKET_ID} results uploaded, but marking success failed." >&2
    echo "       Inspect DSS status before resubmitting." >&2
  fi

done
