"""Main TRACE PoC API layer."""
import hashlib
import json
import os
import random
import re
import shutil
import signal
import string
import subprocess
import uuid
import tempfile
import zipfile

import bagit
from bdbag import bdbag_api as bdb
import cryptography.exceptions
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import docker
from flask import Flask, stream_with_context, request, send_from_directory

app = Flask(__name__)
TMP_PATH = os.path.join(os.environ.get("HOSTDIR", "/"), "tmp")
CERTS_PATH = os.environ.get("TRACE_CERTS_PATH", os.path.abspath("../volumes/certs"))
PRIV_KEY_FILE = os.path.join(CERTS_PATH, "private_key")
STORAGE_PATH = os.environ.get(
    "TRACE_STORAGE_PATH", os.path.abspath("../volumes/storage")
)
TRACE_CLAIMS_FILE = os.path.join(CERTS_PATH, "claims.json")
if not os.path.isfile(TRACE_CLAIMS_FILE):
    TRACE_CLAIMS = {
        "Platform": "My awesome platform!",
        "ProvidedBy": "Xarthisius",
        "Features": "Ran your code with care and love (even though I would write it better...)"
    }
else:
    TRACE_CLAIMS = json.load(open(TRACE_CLAIMS_FILE, "r"))

if not os.path.isfile(PRIV_KEY_FILE):
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(PRIV_KEY_FILE, "wb") as fp:
        fp.write(private_bytes)

with open(PRIV_KEY_FILE, "rb") as fp:
    SIGNING_KEY = ed25519.Ed25519PrivateKey.from_private_bytes(fp.read())
PUBLIC_KEY = SIGNING_KEY.public_key()


def build_image(payload_zip, temp_dir, image):
    """Part of the workflow resposible for building image."""
    yield "\U0001F64F Start building\n"
    # For WT specific buildpacks we would need to inject env.json
    # with open(os.path.join(temp_dir, "environment.json")) as fp:
    #     json.dump({"config": {"buildpack": "PythonBuildPack"}}, fp)
    shutil.unpack_archive(payload_zip, temp_dir, "zip")
    target_repo_dir = "/home/jovyan/work/workspace"
    container_user = "jovyan"
    extra_args = ""
    op = "--no-run"
    letters = string.ascii_lowercase
    image["tag"] = f"local/{''.join(random.choice(letters) for i in range(8))}"
    r2d_cmd = (
        f"jupyter-repo2docker --engine dockercli "
        "--config='/wholetale/repo2docker_config.py' "
        f"--target-repo-dir='{target_repo_dir}' "
        f"--user-id=1000 --user-name={container_user} "
        f"--no-clean {op} --debug {extra_args} "
        f"--image-name {image['tag']} {temp_dir}"
    )
    volumes = {
        "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
        "/tmp": {"bind": TMP_PATH, "mode": "ro"},
    }

    cli = docker.from_env()
    container = cli.containers.run(
        image="wholetale/repo2docker_wholetale:latest",
        command=r2d_cmd,
        environment=["DOCKER_HOST=unix:///var/run/docker.sock"],
        privileged=True,
        detach=True,
        remove=True,
        volumes=volumes,
    )
    for line in container.logs(stream=True):
        yield line.decode("utf-8")
    ret = container.wait()
    if ret["StatusCode"] != 0:
        raise RuntimeError("Error building image")
    yield "\U0001F64C Finished building\n"


def run(temp_dir, image):
    """Part of the workflow running recorded run."""
    yield "\U0001F44A Start running\n"
    cli = docker.from_env()
    container = cli.containers.create(
        image=image["tag"],
        command=f"sh {image['entrypoint']}",
        detach=True,
        volumes={
            temp_dir: {"bind": "/home/jovyan/work/workspace", "mode": "rw"},
        },
    )
    cmd = [
        os.path.join(os.path.join(os.environ.get("HOSTDIR", "/"), "usr/bin/docker")),
        "stats",
        "--format",
        '"{{.CPUPerc}},{{.MemUsage}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}"',
        container.id,
    ]

    dstats_tmppath = os.path.join(temp_dir, ".docker_stats.tmp")
    with open(dstats_tmppath, "w") as dstats_fp:
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)
        p2 = subprocess.Popen(
            ["ts", '"%Y-%m-%dT%H:%M:%.S"'], stdin=p1.stdout, stdout=dstats_fp
        )
        p1.stdout.close()

        container.start()
        for line in container.logs(stream=True):
            yield line.decode("utf-8")

        ret = container.wait()

        p1.send_signal(signal.SIGTERM)
    p2.wait()
    p1.wait()

    with open(os.path.join(temp_dir, ".stdout"), "wb") as fp:
        fp.write(container.logs(stdout=True, stderr=False))
    with open(os.path.join(temp_dir, ".stderr"), "wb") as fp:
        fp.write(container.logs(stdout=False, stderr=True))
    with open(os.path.join(temp_dir, ".entrypoint"), "w") as fp:
        fp.write(image["entrypoint"])
    # Remove 'clear screen' special chars from docker stats output
    # and save it as new file
    with open(dstats_tmppath, "r") as infp:
        with open(dstats_tmppath[:-4], "w") as outfp:
            for line in infp.readlines():
                outfp.write(re.sub(r"\x1b\[2J\x1b\[H", "", line))
    os.remove(dstats_tmppath)
    container.remove()
    if ret["StatusCode"] != 0:
        return "Error executing recorded run", 500
    yield "\U0001F918 Finished running\n"


def _get_manifest_hash(path):
    manifest_hash = hashlib.md5()
    for alg in ["md5", "sha256"]:
        with open(f"{path}/manifest-{alg}.txt", "rb") as fp:
            manifest_hash.update(fp.read())
    return manifest_hash


def generate_tro(payload_zip, temp_dir):
    """Part of the workflow generating TRO..."""
    yield "\U0001F45B Baging result\n"
    bdb.make_bag(temp_dir, metadata=TRACE_CLAIMS.copy())
    payload_zip = f"{payload_zip[:-4]}_run"
    shutil.make_archive(payload_zip, "zip", temp_dir)
    digest = _get_manifest_hash(temp_dir).hexdigest().encode()
    shutil.rmtree(temp_dir)
    yield "\U0001F4DC Signing the bag\n"
    with zipfile.ZipFile(f"{payload_zip}.zip", mode="a") as zf:
        zf.comment = SIGNING_KEY.sign(digest)
    yield (
        "\U0001F4E9 Your magic bag is available as: "
        f"{os.path.basename(payload_zip)}.zip!\n"
    )


@stream_with_context
def magic(payload_zip, entrypoint="run.sh"):
    """Full workflow."""
    temp_dir = tempfile.mkdtemp(dir=TMP_PATH)
    os.chmod(temp_dir, 0o777)  # FIXME: figure out all the uid/gid dance..
    image = {"entrypoint": entrypoint}
    yield from build_image(payload_zip, temp_dir, image)
    yield from run(temp_dir, image)
    yield from generate_tro(payload_zip, temp_dir)
    yield "\U0001F4A3 Done!!!"


@app.route("/", methods=["POST"])
def handler():
    """Either saves payload passed as body or accepts a path to a directory."""
    fname = os.path.join(STORAGE_PATH, f"{str(uuid.uuid4())}.zip")
    if path := request.args.get("path", default="", type=str):
        # Code below is a potential security issue, better not to do it.
        path = os.path.join(os.environ.get("HOSTDIR", "/host"), os.path.abspath(path))
        if not os.path.isdir(path):
            return f"Invalid path: {path}", 400
        shutil.make_archive(fname[:-4], "zip", path)
    if "file" in request.files:
        request.files["file"].save(fname)
    entrypoint = request.args.get("entrypoint", default="run.sh", type=str)
    return magic(fname, entrypoint=entrypoint)


@app.route("/run/<path:path>", methods=["GET"])
def send_run(path):
    """Serve static files from storage dir."""
    return send_from_directory(STORAGE_PATH, path)


@app.route("/verify", methods=["POST"])
def verify_bag():
    """Verify that uploaded bag is signed and valid."""
    if "file" not in request.files:
        return "No bag found", 400
    fname = os.path.join(TMP_PATH, f"{str(uuid.uuid4())}.zip")
    request.files["file"].save(fname)
    temp_dir = tempfile.mkdtemp(dir=TMP_PATH)
    shutil.unpack_archive(fname, temp_dir, "zip")
    try:
        bdb.validate_bag(temp_dir)
    except bagit.BagError:
        return "Invalid bag", 400
    except bagit.BagValidationError:
        return "Bag failed validation", 400

    digest = _get_manifest_hash(temp_dir).hexdigest().encode()
    with zipfile.ZipFile(fname, mode="r") as zf:
        try:
            PUBLIC_KEY.verify(zf.comment, digest)
        except cryptography.exceptions.InvalidSignature:
            return "Invalid signature", 400
    return "\U00002728 Valid and signed bag!"
