import argparse

from .providers import FakeModelClient
from .runtime import Nagi
from .state import RunStore
from .workspace import Workspace


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Teaching-sized Nagi agent harness.")
    parser.add_argument("prompt", nargs="*", help="One-shot prompt. If omitted, mini-nagi starts a small REPL.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--approval", choices=("auto", "never"), default="auto", help="Whether risky tools are allowed.")
    parser.add_argument("--max-steps", type=int, default=4, help="Maximum tool/model iterations.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    return parser


def build_agent(args):
    workspace = Workspace.build(args.cwd)
    run_store = RunStore(workspace.root / ".mini-nagi" / "runs")
    return Nagi(
        model_client=FakeModelClient(),
        workspace=workspace,
        run_store=run_store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
    )


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)
    prompt = " ".join(args.prompt).strip()
    if prompt:
        print(agent.ask(prompt))
        return 0

    while True:
        try:
            user_input = input("mini-nagi> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0
        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        print(agent.ask(user_input))
