import os
import re
import shutil
import signal
import subprocess
import uuid
import tempfile
import time

import docker
from flask import Flask, stream_with_context, request

app = Flask(__name__)
TMP_PATH = os.path.join(os.environ.get("HOSTDIR", "/"), "tmp")


def build_image(payload_zip, temp_dir):
    yield "Start building\n"
    # For WT specific buildpacks we would need to inject env.json
    # with open(os.path.join(temp_dir, "environment.json")) as fp:
    #     json.dump({"config": {"buildpack": "PythonBuildPack"}}, fp)
    shutil.unpack_archive(payload_zip, temp_dir, "zip")
    target_repo_dir = "/home/jovyan/work/workspace"
    container_user = "jovyan"
    extra_args = ""
    op = "--no-run"
    tag = "local/foo"
    r2d_cmd = (
        f"jupyter-repo2docker --engine dockercli "
        "--config='/wholetale/repo2docker_config.py' "
        f"--target-repo-dir='{target_repo_dir}' "
        f"--user-id=1000 --user-name={container_user} "
        f"--no-clean {op} --debug {extra_args} "
        f"--image-name {tag} {temp_dir}"
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
        return "Error building image", 500
    yield "Finished building\n"


def run(temp_dir):
    yield "Start running\n"
    entrypoint = "run.sh"  # FIXME
    cli = docker.from_env()
    container = cli.containers.create(
        image="local/foo",
        command=f"sh {entrypoint}",
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
        container.id
    ]

    dstats_tmppath = os.path.join(temp_dir, ".docker_stats.tmp")
    with open(dstats_tmppath, "w") as dstats_fp:
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)
        p2 = subprocess.Popen(["ts", '"%Y-%m-%dT%H:%M:%.S"'], stdin=p1.stdout, stdout=dstats_fp)
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
        fp.write(entrypoint)
    # Remove 'clear screen' special chars from docker stats output
    # and save it as new file
    with open(dstats_tmppath, "r") as infp:
        with open(dstats_tmppath[:-4], "w") as outfp:
            for line in infp.readlines():
                outfp.write(re.sub(r"\x1b\[2J\x1b\[H", "", line))
    os.remove(dstats_tmppath)
    container.remove()
    if ret['StatusCode'] != 0:
        return 'Error executing recorded run', 500
    yield "Finished running\n"


def generate_tro():
    yield "Performing the magic\n"
    time.sleep(1)


@stream_with_context
def magic(payload_zip):
    temp_dir = tempfile.mkdtemp(dir=TMP_PATH)
    os.chmod(temp_dir, 0o777)  # FIXME: figure out all the uid/gid dance..
    yield from build_image(payload_zip, temp_dir)
    yield from run(temp_dir)
    yield from generate_tro()
    payload_zip = f"{payload_zip[:-4]}_run"
    shutil.make_archive(payload_zip, "zip", temp_dir)
    shutil.rmtree(temp_dir)
    yield f"Your magic bag is available as {payload_zip}.zip!\n"


@app.route("/", methods=["POST"])
def handler():
    """Either saves payload passed as body or accepts a path to a directory."""
    fname = f"/tmp/{str(uuid.uuid4())}.zip"
    if path := request.args.get("path", default="", type=str):
        # Code below is a potential security issue, better not to do it.
        path = os.path.join(os.environ.get("HOSTDIR", "/host"), os.path.abspath(path))
        if not os.path.isdir(path):
            return f"Invalid path: {path}", 400
        shutil.make_archive(fname, "zip", path)
    if "file" in request.files:
        request.files["file"].save(fname)
    return magic(fname)
