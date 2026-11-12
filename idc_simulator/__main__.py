import argparse
import asyncio
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="IDC state-driven SNMP/SSH simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--topology")
    serve.add_argument("--state-dir", default=".sim")
    serve.add_argument("--mode", choices=("local", "namespace"), default="local")
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--api-port", type=int, default=8080)
    serve.add_argument("--snmp-port", type=int, default=1161)
    serve.add_argument("--ssh-port", type=int, default=8022)
    serve.add_argument("--worker-uid", type=int)
    serve.add_argument("--model-only", action="store_true", help="Serve Web/API and topology models without SNMP/SSH workers")
    serve.add_argument("--allow-unauthenticated", action="store_true",
                       help="Allow a non-loopback bind without IDC_API_TOKEN (isolated labs only)")
    validate = sub.add_parser("validate")
    validate.add_argument("topology")
    sub.add_parser("profiles")
    plan = sub.add_parser("network-plan")
    plan.add_argument("topology")
    plan.add_argument("--remove", action="store_true")
    generate = sub.add_parser("generate")
    generate.add_argument("--devices", type=int, default=100)
    generate.add_argument("--ports", type=int, default=48)
    generate.add_argument("--output", required=True)
    generate.add_argument("--local", action="store_true")
    generate.add_argument("--layout", choices=("chain", "tree", "ring", "mixed"), default="chain")
    gpu = sub.add_parser("generate-gpu-idc", help="64 GPU servers, 2 storage servers and 16 switches")
    gpu.add_argument("--output", required=True)
    bench = sub.add_parser("benchmark")
    bench.add_argument("topology")
    bench.add_argument("--duration", type=float, default=60)
    bench.add_argument("--period", type=float, default=30)
    bench.add_argument("--concurrency", type=int, default=20)
    bench.add_argument("--v3-percent", type=float, default=0)
    bench.add_argument("--ssh-concurrency", type=int, default=0)
    bench.add_argument("--snmp-port", type=int, default=161)
    bench.add_argument("--ssh-port", type=int, default=22)
    bench.add_argument("--timeout", type=float, default=1)
    bench.add_argument("--retries", type=int, default=0)
    bench.add_argument("--max-repetitions", type=int, default=25)
    bench.add_argument("--walk-root")
    bench.add_argument("--output", default="benchmark.json")
    storm = sub.add_parser("pressure")
    storm.add_argument("topology")
    storm.add_argument("--api", default="http://127.0.0.1:8080")
    storm.add_argument("--rate", type=int, default=100)
    storm.add_argument("--duration", type=float, default=60)
    storm.add_argument("--output", default="pressure.json")
    receiver = sub.add_parser("receive")
    receiver.add_argument("--topology")
    receiver.add_argument("--bind", default="127.0.0.1")
    receiver.add_argument("--port", type=int, default=10162)
    receiver.add_argument("--duration", type=float, default=60)
    receiver.add_argument("--expected", type=int)
    receiver.add_argument("--output", default="received.json")
    worker = sub.add_parser("worker")
    worker.add_argument("--device", required=True)
    worker.add_argument("--socket", required=True)
    worker.add_argument("--state-dir", required=True)
    worker.add_argument("--snmp-port", type=int, required=True)
    worker.add_argument("--ssh-port", type=int, required=True)
    worker.add_argument("--uid", type=int)
    worker.add_argument("--role", choices=("query", "notification"), default="query")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from idc_simulator.runtime import serve
            asyncio.run(serve(args))
        elif args.command == "worker":
            from idc_simulator.worker import run_worker
            asyncio.run(run_worker(args))
        elif args.command == "profiles":
            from idc_simulator.profiles import list_profiles
            print(json.dumps([{ "id": p["id"], **p["compatibility"]} for p in list_profiles()], indent=2))
        elif args.command == "generate":
            import yaml
            from idc_simulator.tools import generate_lab
            Path(args.output).write_text(yaml.safe_dump(generate_lab(args.devices, args.ports, args.local, args.layout), sort_keys=False))
        elif args.command == "generate-gpu-idc":
            import yaml
            from idc_simulator.idc_lab import generate_gpu_idc
            Path(args.output).write_text(yaml.safe_dump(generate_gpu_idc(), sort_keys=False))
        elif args.command == "benchmark":
            if args.duration <= 0 or args.period < 0 or args.concurrency <= 0 or not 0 <= args.v3_percent <= 100:
                raise ValueError("Invalid benchmark parameters")
            from idc_simulator.benchmark import benchmark
            asyncio.run(benchmark(args))
        elif args.command in {"pressure", "receive"}:
            from idc_simulator.loadtools import pressure, receive
            if args.duration <= 0 or (args.command == "pressure" and args.rate <= 0):
                raise ValueError("Duration and rate must be positive")
            asyncio.run(pressure(args) if args.command == "pressure" else receive(args))
        else:
            from idc_simulator.model import load_lab
            lab = load_lab(args.topology)
            if args.command == "network-plan":
                from idc_simulator.deploy import network_plan
                import shlex
                print("\n".join(shlex.join(argv) for argv in network_plan(lab, args.remove)))
            else:
                print(json.dumps({"name": lab["name"], "devices": len(lab["devices"]), "links": len(lab["links"]), "valid": True}))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
