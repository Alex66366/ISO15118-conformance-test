# Universal EXI Bridge — Python Microservice
## ISO 15118-2/20 EXI Codec via Siemens EXIfficient (self-contained, vendored)

### Architecture

```
C++ V2G_TCP_TLS_Port.cc (Dumb Terminal)
       |  send("ENCODE\n<ns>\n<json>\n\n")
       v
localhost:9000  (this service, Python)
       |  py4j call
       v
JVM + vendor/iso15118/shared/EXICodec.jar
       |  EXI encode/decode (using upstream Siemens EXIfficient)
       v
EXI bytes  <->  JSON string
```

**Key design point**: the EXI codec (Python bridge + Siemens EXIficient JAR) is
now fully **vendored inside this package** under `vendor/iso15118/`. The
microservice does **NOT** require an external `iso15118-master` checkout at
runtime — the upstream Siemens EXIficient JAR is bundled here, along with the
four small Python files needed to wrap it.

### Prerequisites

| Dependency | Recommended | Alt |
|---|---|---|
| Python | `conda run -n iso15118` | `python3` (system) |
| py4j | `conda env iso15118` has it | `pip install py4j` |
| Java | OpenJDK 11+ (system) | `sudo apt install openjdk-11-jre-headless` |
| EXICodec.jar | Bundled in `vendor/iso15118/shared/` (13 MB) | No external copy needed |

### Quick Start

```bash
# 1. (First time only) Verify the vendored jar is present
ls -lh shared_infrastructure/exi_microservice/vendor/iso15118/shared/EXICodec.jar

# 2. Start the microservice using the iso15118 conda environment
cd agent/test_code/shared_infrastructure/exi_microservice
./start_exi_service.sh
# Or manually:
#   conda run -n iso15118 python3 exi_microservice.py &
#   conda run -n iso15118 python3 exi_microservice.py --port 9000

# 3. Verify it's running
nc -z localhost 9000 && echo "OK: port 9000 is open"

# 4. Stop
./stop_exi_service.sh
```

The service will start with **zero environment-variable setup**. There is no
need to clone `iso15118-master` anywhere.

### Vendor layout

```
shared_infrastructure/exi_microservice/
├── exi_microservice.py            # TCP server (unchanged)
├── exi_codec_handler.py           # Py4j bridge (vendor-first resolution)
├── vendor/                        # self-contained EXI codec
│   └── iso15118/
│       ├── __init__.py            # version constant
│       └── shared/
│           ├── __init__.py        # empty (no logger init)
│           ├── exificient_exi_codec.py  # upstream wrapper
│           ├── iexi_codec.py             # upstream interface
│           ├── settings.py               # JAR_FILE_PATH resolver
│           └── EXICodec.jar              # Siemens EXIfficient (~13 MB)
├── iso15118_grammars.json         # namespace -> protocol mapping
├── schemas/                       # XSDs (used by smoke_test)
├── EXICodec.jar                   # legacy copy (kept for fallback; can be deleted)
├── README.md                      # this file
├── smoke_test.sh                  # TCP smoke test
├── start_exi_service.sh
├── stop_exi_service.sh
└── requirements.txt
```

### Asset resolution priority

`exi_codec_handler.py` resolves both the JAR path and the Python bridge
location using this priority order (first hit wins):

| Priority | JAR (`EXI_JAR_PATH`) | Python bridge (`EXI_BRIDGE_DIR`) |
|---|---|---|
| 1 (operator override) | `$EXI_JAR_PATH` env var | `$EXI_BRIDGE_DIR` env var |
| 2 (preferred) | `vendor/iso15118/shared/EXICodec.jar` | `vendor/` (parent of `iso15118/`) |
| 3 (legacy) | `$ISO15118_MASTER_PATH/iso15118/shared/EXICodec.jar` | `$ISO15118_MASTER_PATH` |
| 4 (legacy) | `<parent>/iso15118-master/iso15118/shared/EXICodec.jar` | `<parent>/iso15118-master` |
| 5 (legacy) | `/home/dmc/桌面/platform/iso15118-master/iso15118/shared/EXICodec.jar` | `/home/dmc/桌面/platform/iso15118-master` |

If **none** of these exist, the service fails fast with a clear error message
pointing to the candidates it tried. The legacy paths (3-5) are kept so users
who still want to consume upstream sources from a sibling checkout continue to
work without code changes.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `EXI_PORT` | `9000` | TCP port to listen on |
| `EXI_LOG_LEVEL` | `INFO` | Python logging level: DEBUG, INFO, WARNING, ERROR |
| `EXI_JAR_PATH` | *(unset)* | Override the EXICodec.jar location |
| `EXI_BRIDGE_DIR` | *(unset)* | Override the iso15118 Python bridge location |
| `EXI_IDLE_TIMEOUT` | `300` | TCP server idle timeout in seconds |
| `ISO15118_MASTER_PATH` | *(unset)* | Legacy: path to iso15118-master checkout |
| `CONDA_ENV` | `iso15118` | Conda environment to use (if conda is available) |
| `PYTHON` | *(auto-detect)* | Override Python interpreter (e.g. `python3`) |
| `LOG_FILE` | `/tmp/exi_microservice.log` | Log file location |

### Protocol (TCP, newline-framed)

```
Encode request:  "ENCODE\n<namespace>\n<json>\n\n"
Encode OK:     "OK\n<exi_hex>\n\n"
Encode ERR:    "ERROR\n<message>\n\n"

Decode request:  "DECODE\n<namespace>\n<exi_hex>\n\n"
Decode OK:     "OK\n<json>\n\n"
Decode ERR:    "ERROR\n<message>\n\n"
```

### Namespaces

| Namespace | Protocol | Schema |
|---|---|---|
| `urn:iso:15118:2:2013:MsgDef` | ISO 15118-2 V2G Messages | `iso15118_2/V2G_CI_MsgDef.xsd` |
| `urn:iso:15118:2:2013:appProtocol` | SupportedAppProtocol | `V2G_CI_AppProtocol.xsd` |
| `urn:iso:15118:20:commonMessages` | ISO 15118-20 | `iso15118_20/V2G_CI_CommonMessages.xsd` |
| `urn:din:70121:x` | DIN SPEC 70121 | `din_spec/V2G_CI_MsgDef.xsd` |

### How to update the vendored JAR

The Siemens EXIfficient JAR is binary-stable across minor upstream releases.
When (if) a new version is released:

```bash
# 1. Drop the new jar into vendor/
cp /path/to/new/EXICodec.jar vendor/iso15118/shared/EXICodec.jar

# 2. (Optional) Mirror to iso15118-master if you still maintain that repo
cp /path/to/new/EXICodec.jar /path/to/iso15118-master/iso15118/shared/EXICodec.jar

# 3. Restart the EXI service
./stop_exi_service.sh && ./start_exi_service.sh
```

No code changes are required — the resolution priority will pick the new jar
automatically.

### Why we vendor instead of importing from iso15118-master

1. **Decoupling**: `agent/test_code/` no longer requires a sibling
   `iso15118-master/` directory to run. The test framework is self-contained.
2. **Version pinning**: We can roll forward/backward without touching the
   upstream repo, avoiding surprise breakage.
3. **CI portability**: A single directory under `agent/` is enough; no
   `git submodule update` or external `git clone` step.
4. **Backward compatibility preserved**: The legacy fallback paths
   (`ISO15118_MASTER_PATH`, sibling-directory convention) still work, so
   existing scripts that point to the upstream repo continue to function.