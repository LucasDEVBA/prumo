#!/usr/bin/env bash
# Gera build/lambda: dependências de produção para Lambda arm64 (Python 3.12) + o pacote prumo.
#
# Uso: bash scripts/build_lambda.sh
# As versões saem do uv.lock da raiz (--locked: lock desatualizado em relação ao pyproject.toml
# quebra o build) e cada wheel é conferido pelo hash do lock. No CI (CI=true) o lock é
# obrigatório; localmente, sem lock, resolve na hora com aviso (build não reprodutível).
# Variáveis opcionais:
#   UV=/caminho/uv                 binário do uv (padrão: o do PATH ou .venv/bin/uv)
#   LAMBDA_PYTHON_PLATFORM=...     alvo dos wheels (padrão: aarch64-manylinux_2_34)
#   PRUMO_STRIP_BOTO=1             remove boto3/botocore/s3transfer do pacote (ver abaixo)
set -Eeuo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly BUILD_DIR="${ROOT_DIR}/build"
readonly OUT_DIR="${BUILD_DIR}/lambda"
readonly PYTHON_VERSION="3.12"
# A Lambda python3.12 roda em Amazon Linux 2023 (glibc 2.34). Mirar exatamente essa glibc aceita
# wheels manylinux2014, _2_28 e _2_34; mirar manylinux2014 (glibc 2.17) não serve mais porque o
# numpy 2.5+ só publica wheels aarch64 manylinux_2_28.
readonly PYTHON_PLATFORM="${LAMBDA_PYTHON_PLATFORM:-aarch64-manylinux_2_34}"
readonly STRIP_BOTO="${PRUMO_STRIP_BOTO:-0}"
# Limite da Lambda para o pacote descompactado (código + camadas).
readonly MAX_UNZIPPED_BYTES=$((250 * 1024 * 1024))

log() { printf '[build_lambda] %s\n' "$*" >&2; }
fail() {
  log "ERRO: $*"
  exit 1
}
trap 'fail "falhou na linha ${LINENO}: ${BASH_COMMAND}"' ERR

resolve_uv() {
  if [[ -n "${UV:-}" ]]; then
    printf '%s\n' "${UV}"
  elif command -v uv >/dev/null 2>&1; then
    command -v uv
  elif [[ -x "${ROOT_DIR}/.venv/bin/uv" ]]; then
    printf '%s\n' "${ROOT_DIR}/.venv/bin/uv"
  else
    fail "uv não encontrado: instale (https://docs.astral.sh/uv) ou defina UV=/caminho/uv"
  fi
}

export_requirements() {
  local uv="$1" output="$2"
  if [[ -f "${ROOT_DIR}/uv.lock" ]]; then
    # Sem extras nem grupo dev: o uvicorn (extra "server") não entra no pacote da Lambda.
    log "exportando dependências de produção do uv.lock"
    "${uv}" export --project "${ROOT_DIR}" --locked --no-dev --no-emit-project \
      --format requirements-txt --quiet --output-file "${output}"
    return 0
  fi
  if [[ "${CI:-}" == "true" ]]; then
    fail "uv.lock ausente: no CI o pacote só sai do lock (rode 'uv lock' e faça commit dele)"
  fi
  # Sem lock, resolve para a plataforma da Lambda (e não para a máquina de quem roda o build).
  log "AVISO: sem uv.lock, resolvendo o pyproject.toml agora (versões podem mudar entre builds)"
  "${uv}" pip compile "${ROOT_DIR}/pyproject.toml" --python-platform "${PYTHON_PLATFORM}" \
    --python-version "${PYTHON_VERSION}" --generate-hashes --no-header --no-annotate --quiet \
    --output-file "${output}"
}

install_into_bundle() {
  local uv="$1"
  shift
  # --link-mode=copy: hardlinks para o cache do uv deixariam o bundle e o cache acoplados.
  "${uv}" pip install --target "${OUT_DIR}" --python-platform "${PYTHON_PLATFORM}" \
    --python-version "${PYTHON_VERSION}" --link-mode=copy --quiet "$@"
}

strip_boto_if_requested() {
  # Mantemos o SDK no pacote por padrão: o boto3 do runtime atrasa em relação às APIs do
  # Bedrock (Converse, ApplyGuardrail) e misturar versões do botocore com o urllib3 do pacote é
  # fonte de erro difícil de reproduzir. Remover só economiza ~25 MB e vale apenas se o limite
  # de 250 MB apertar.
  if [[ "${STRIP_BOTO}" != "1" ]]; then
    return 0
  fi
  log "PRUMO_STRIP_BOTO=1: removendo boto3/botocore/s3transfer (o runtime fornece os seus)"
  find "${OUT_DIR}" -maxdepth 1 \( -name 'boto3*' -o -name 'botocore*' -o -name 's3transfer*' \) \
    -exec rm -rf {} +
}

clean_bundle() {
  find "${OUT_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} +
  # Scripts de console apontam para o Python local e não servem na Lambda.
  rm -rf "${OUT_DIR:?}/bin"
}

check_bundle() {
  [[ -f "${OUT_DIR}/prumo/__init__.py" ]] || fail "o pacote prumo não foi instalado no bundle"
  # Servidor local não tem papel na Lambda (a API roda pelo Mangum); se aparecer, alguém o
  # devolveu às dependências base do pyproject.toml.
  [[ ! -d "${OUT_DIR}/uvicorn" ]] || fail "uvicorn no bundle: ele pertence ao extra 'server'"
  local size_bytes
  size_bytes=$(($(du -sk "${OUT_DIR}" | cut -f1) * 1024))
  if ((size_bytes > MAX_UNZIPPED_BYTES)); then
    fail "bundle com $((size_bytes / 1024 / 1024)) MB passa do limite de 250 MB da Lambda"
  fi
  log "bundle pronto em ${OUT_DIR}: $(du -sh "${OUT_DIR}" | cut -f1) descompactado"
}

main() {
  local uv requirements
  uv="$(resolve_uv)"
  requirements="$(mktemp)"
  # shellcheck disable=SC2064 # o caminho precisa ser expandido agora, não na saída.
  trap "rm -f '${requirements}'" EXIT

  rm -rf "${OUT_DIR:?}"
  mkdir -p "${OUT_DIR}"
  # Mantém o diretório de build fora do git sem depender do .gitignore da raiz.
  printf '*\n' >"${BUILD_DIR}/.gitignore"

  export_requirements "${uv}" "${requirements}"
  log "instalando dependências (só wheels binários, ${PYTHON_PLATFORM}, Python ${PYTHON_VERSION})"
  install_into_bundle "${uv}" --only-binary=:all: --require-hashes --requirement "${requirements}"
  log "instalando o pacote prumo"
  install_into_bundle "${uv}" --no-deps "${ROOT_DIR}"

  strip_boto_if_requested
  clean_bundle
  check_bundle
}

main "$@"
