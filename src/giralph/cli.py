#!/usr/bin/env python3
"""giralph CLI — run from any project directory via `uvx giralph`."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from importlib import resources

# Agent definitions.
# "interactive" agents get full terminal control (no capture).
# "headless" agents pipe stdin and capture stdout.
AGENTS = {
    "claude-code": {
        "bin": "claude",
        "interactive": True,
        "core": True,
        "role": "implementation",
    },
    "codex": {
        "bin": "codex",
        "cmd": ["codex", "exec", "-"],
        "interactive": False,
        "core": True,
        "role": "thinking",
    },
    "gemini-cli": {
        "bin": "gemini",
        "cmd": ["gemini"],
        "interactive": False,
        "core": True,
        "role": "multilingual",
    },
    "qwen-code": {
        "bin": "qwen",
        "cmd": ["qwen", "-p", "-"],
        "interactive": False,
        "core": False,
        "role": "creative",
    },
    "opencode": {
        "bin": "opencode",
        "cmd": ["opencode", "run"],
        "interactive": False,
        "core": False,
        "role": "general",
    },
}

# Priority order for auto-selecting the primary agent
AGENT_PRIORITY = ["claude-code", "codex", "gemini-cli", "qwen-code", "opencode"]


def detect_agents():
    """Check which agents are installed. Returns {name: bool}."""
    return {name: shutil.which(spec["bin"]) is not None for name, spec in AGENTS.items()}


def print_agents(available):
    """Print detected agents with status."""
    for name in AGENT_PRIORITY:
        spec = AGENTS[name]
        found = available[name]
        tag = "core" if spec["core"] else "optional"
        icon = "+" if found else "-"
        status = "found" if found else "not found"
        print(f"  [{icon}] {name:15s} ({spec['role']:14s}) {tag:8s}  {status}")


def pick_agent(preferred, available):
    """Pick the best available agent. Returns name or exits if none found."""
    if preferred and available.get(preferred):
        return preferred
    if preferred and not available.get(preferred):
        print(f"[giralph] Warning: preferred agent '{preferred}' not found.")
    # Fall through priority list
    for name in AGENT_PRIORITY:
        if available[name]:
            if preferred:
                print(f"[giralph] Falling back to: {name}")
            return name
    print("[giralph] Error: no coding agents found. Install at least one of: claude, codex, gemini")
    sys.exit(1)


def filter_debate_agents(agents, available):
    """Remove unavailable agents from debate list, warn about them."""
    valid = []
    for name in agents:
        if name not in AGENTS:
            print(f"[giralph] Warning: unknown agent '{name}' in debate list, skipping.")
        elif not available[name]:
            print(f"[giralph] Warning: '{name}' not found, removing from debate.")
        else:
            valid.append(name)
    return valid


def get_work_dir():
    """Work dir is always CWD — giralph runs where you invoke it."""
    return os.getcwd()


def check_telegram_configured():
    """Check if Claude Code's telegram channel is set up. Returns (configured, reason)."""
    env_path = os.path.expanduser("~/.claude/channels/telegram/.env")
    if not os.path.exists(env_path):
        return False, "no telegram bot token found"
    try:
        with open(env_path, "r") as f:
            content = f.read()
        if "TELEGRAM_BOT_TOKEN=" not in content:
            return False, "telegram .env exists but no bot token set"
        token = content.split("TELEGRAM_BOT_TOKEN=", 1)[1].split("\n")[0].strip()
        if not token or token.startswith("your-"):
            return False, "telegram bot token is a placeholder"
    except Exception:
        return False, "could not read telegram config"
    return True, "configured"


def prompt_telegram_setup():
    """Guide user to set up telegram before running giralph."""
    print()
    print("[giralph] Telegram is not configured for Claude Code.")
    print("[giralph] giralph uses Telegram for bidirectional communication with you.")
    print()
    print("  To set up:")
    print("  1. Message @BotFather on Telegram to create a bot")
    print("  2. Copy the bot token")
    print("  3. Run: claude /telegram:configure")
    print("  4. Paste your bot token when prompted")
    print("  5. Message your bot on Telegram to pair your account")
    print()
    answer = input("[giralph] Continue without Telegram? (y/N): ").strip().lower()
    return answer in ("y", "yes")


def read_file(name, work_dir=None):
    """Read a file from work_dir, return empty string if missing."""
    path = os.path.join(work_dir or get_work_dir(), name)
    if not os.path.exists(path):
        return ""
    with open(path, "r") as f:
        return f.read()


def write_file(name, content, work_dir=None):
    """Write content to a file in work_dir."""
    path = os.path.join(work_dir or get_work_dir(), name)
    with open(path, "w") as f:
        f.write(content)


def load_config(work_dir=None):
    """Load config.json from work_dir, return defaults if missing."""
    wd = work_dir or get_work_dir()
    path = os.path.join(wd, "config.json")
    defaults = {
        "agent": "claude-code",
        "max_iterations": 0,
        "cooldown_seconds": 5,
        "debate_agents": [],
        "debate_judge": "claude-code",
    }
    if os.path.exists(path):
        with open(path, "r") as f:
            cfg = json.load(f)
        defaults.update(cfg)
    return defaults


def build_prompt(instruction, memory, plan, prompt):
    """Assemble the full prompt from component files."""
    parts = []
    if instruction:
        parts.append(instruction)
    if memory:
        parts.append(f"<memory>\n{memory}\n</memory>")
    if plan:
        parts.append(f"<plan>\n{plan}\n</plan>")
    if prompt:
        parts.append(f"<task>\n{prompt}\n</task>")
    return "\n\n".join(parts)


def parse_status(output):
    """Parse GIRALPH_STATUS block from agent output. Returns dict or None."""
    match = re.search(r"GIRALPH_STATUS:\s*\n(.*?)(?:\n\s*$|\Z)", output, re.DOTALL)
    if not match:
        return None
    status = {}
    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            status[key.strip()] = value.strip()
    return status


TMUX_SESSION = "giralph"
TELEGRAM_CHANNEL = "plugin:telegram@claude-plugins-official"


def build_claude_prompt(work_dir, iteration):
    """Build the @file prompt for claude's interactive TUI."""
    file_refs = []
    for name in ["INSTRUCTION.md", "MEMORY.md", "PLAN.md", "PROMPT.md"]:
        path = os.path.join(work_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            file_refs.append(f"@{name}")

    prompt = " ".join(file_refs)
    if iteration == 1:
        prompt += " Check telegram for new messages and instructions first."
    else:
        prompt += " Check telegram for new messages. Continue with the plan."
    return prompt


def run_claude_tmux(work_dir, iteration):
    """Run claude interactively in a tmux session. Returns when claude exits."""
    done_file = os.path.join(work_dir, ".giralph_done")

    # Clean up from previous iteration
    if os.path.exists(done_file):
        os.remove(done_file)

    # Kill stale session if exists
    subprocess.run(
        ["tmux", "kill-session", "-t", TMUX_SESSION],
        capture_output=True,
    )

    # Build claude command with correct --channels plugin syntax
    tg_ok, _ = check_telegram_configured()
    claude_cmd = "claude --dangerously-skip-permissions"
    if tg_ok:
        claude_cmd += f" --channels {TELEGRAM_CHANNEL}"

    # Launch claude in tmux. Touch done file when claude exits.
    wrapper = f'cd "{work_dir}" && {claude_cmd}; touch "{done_file}"'
    subprocess.run([
        "tmux", "new-session", "-d",
        "-s", TMUX_SESSION,
        "-x", "220", "-y", "50",
        "bash", "-c", wrapper,
    ])

    # Wait for claude's TUI to initialize
    time.sleep(5)

    # Verify the tmux session is alive (claude didn't crash on startup)
    check = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True,
    )
    if check.returncode != 0:
        print(f"[giralph] Claude failed to start. Check the error:")
        # Try to read any output from the crashed session
        if os.path.exists(done_file):
            os.remove(done_file)
        return "error"

    # Send the @file prompt into claude's input field
    prompt = build_claude_prompt(work_dir, iteration)
    subprocess.run([
        "tmux", "send-keys", "-t", TMUX_SESSION,
        prompt, "Enter",
    ])

    print(f"[giralph] Claude running in tmux session '{TMUX_SESSION}'")
    if tg_ok:
        print(f"[giralph] Telegram channel: enabled")
    print(f"[giralph] Attach with:  tmux attach -t {TMUX_SESSION}")
    print(f"[giralph] Waiting for claude to finish...")

    # Poll for completion
    try:
        while not os.path.exists(done_file):
            time.sleep(10)
    except KeyboardInterrupt:
        print(f"\n[giralph] Ctrl+C — killing tmux session...")
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], capture_output=True)
        if os.path.exists(done_file):
            os.remove(done_file)
        return "interrupted"

    os.remove(done_file)
    return "exit_code=0"


def run_agent(agent_name, prompt_text, work_dir, iteration=1):
    """Run an agent. Interactive agents use tmux, headless agents pipe stdin."""
    spec = AGENTS.get(agent_name)
    if not spec:
        print(f"[giralph] Unknown agent: {agent_name}")
        return ""

    if not shutil.which(spec["bin"]):
        print(f"[giralph] Agent '{agent_name}' not available (binary: {spec['bin']})")
        return ""

    if spec["interactive"]:
        if not shutil.which("tmux"):
            print(f"[giralph] tmux is required for interactive agents. Install it with: sudo apt install tmux")
            return ""
        return run_claude_tmux(work_dir, iteration)
    else:
        # Headless mode — pipe stdin, stream stdout
        cmd = list(spec["cmd"])
        output_lines = []
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=work_dir,
            )
            proc.stdin.write(prompt_text)
            proc.stdin.close()

            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                output_lines.append(line)

            proc.wait(timeout=1800)

            if proc.returncode != 0:
                stderr = proc.stderr.read()
                if stderr:
                    print(f"[giralph] {agent_name} stderr: {stderr[:500]}")

            return "".join(output_lines).strip()
        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"[giralph] {agent_name} timed out after 1800s")
            return "".join(output_lines).strip()
        except Exception as e:
            print(f"[giralph] Error running {agent_name}: {e}")
            return ""


def debate(agents, judge, prompt_text, work_dir):
    """Run multiple agents on the same prompt, then have a judge pick the best."""
    print(f"[giralph] Debate mode: {agents} judged by {judge}")
    responses = {}
    for agent_name in agents:
        print(f"[giralph]   Running {agent_name}...")
        responses[agent_name] = run_agent(agent_name, prompt_text, work_dir)

    judge_prompt = "You are judging a debate between coding agents.\n\n"
    judge_prompt += f"Original task:\n{prompt_text}\n\n"
    for name, resp in responses.items():
        judge_prompt += f"--- Response from {name} ---\n{resp}\n\n"
    judge_prompt += (
        "Compare the responses. Pick the best one or synthesize the best parts. "
        "Explain your reasoning briefly, then provide the final answer."
    )

    print(f"[giralph]   Judging with {judge}...")
    verdict = run_agent(judge, judge_prompt, work_dir)
    return verdict, responses


def get_file_mtimes(work_dir):
    """Get modification times of state files for change detection."""
    mtimes = {}
    for name in ["MEMORY.md", "PLAN.md", "PROMPT.md"]:
        path = os.path.join(work_dir, name)
        if os.path.exists(path):
            mtimes[name] = os.path.getmtime(path)
    return mtimes


def log_iteration(iteration, agent, work_dir, files_changed=None, output=""):
    """Append a log entry to HISTORY.md."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    entry = f"\n## Iteration {iteration} — {ts}\n"
    entry += f"**Agent:** {agent}\n"
    if files_changed:
        entry += f"**Files changed:** {', '.join(files_changed)}\n"
    if output and output not in ("exit_code=0", "interrupted"):
        entry += f"\n{output[:500]}\n"
    entry += "\n---\n"

    path = os.path.join(work_dir, "HISTORY.md")
    with open(path, "a") as f:
        f.write(entry)


# ── Subcommands ──────────────────────────────────────────────────────


def cmd_init(args):
    """Scaffold giralph files into the current directory."""
    wd = get_work_dir()
    defaults_dir = resources.files("giralph") / "defaults"

    files = {
        "INSTRUCTION.md": defaults_dir / "INSTRUCTION.md",
        "config.json": defaults_dir / "config.json",
    }

    # Always create these empty if missing
    touch = ["MEMORY.md", "PLAN.md", "PROMPT.md"]

    created = []
    skipped = []

    for name, src in files.items():
        dest = os.path.join(wd, name)
        if os.path.exists(dest) and not args.force:
            skipped.append(name)
            continue
        content = src.read_text()
        with open(dest, "w") as f:
            f.write(content)
        created.append(name)

    for name in touch:
        dest = os.path.join(wd, name)
        if not os.path.exists(dest):
            with open(dest, "w") as f:
                f.write("")
            created.append(name)

    if created:
        print(f"[giralph] Created: {', '.join(created)}")
    if skipped:
        print(f"[giralph] Skipped (already exist): {', '.join(skipped)}")
        print(f"[giralph] Use --force to overwrite.")
    if not created and not skipped:
        print("[giralph] All files already exist.")

    # Detect and display available agents
    available = detect_agents()
    print(f"\n[giralph] Detected agents:")
    print_agents(available)

    primary = pick_agent(None, available)
    core_missing = [n for n in AGENT_PRIORITY if AGENTS[n]["core"] and not available[n]]
    if core_missing:
        print(f"\n[giralph] Tip: install missing core agents for debate mode: {', '.join(core_missing)}")

    # Check telegram
    tg_ok, tg_reason = check_telegram_configured()
    if tg_ok:
        print(f"\n[giralph] Telegram: configured")
    else:
        print(f"\n[giralph] Telegram: {tg_reason}")
        print("[giralph] Run `claude /telegram:configure` to set up Telegram.")

    print(f"\n[giralph] Ready. Primary agent: {primary}")
    print(f"[giralph] Edit PROMPT.md with your task, then run: giralph run")


def cmd_run(args):
    """Main ralph loop with circuit breakers."""
    work_dir = os.path.abspath(args.work_dir or get_work_dir())
    config = load_config(work_dir)

    max_iter = args.max_iterations if args.max_iterations is not None else config.get("max_iterations", 0)
    cooldown = args.cooldown if args.cooldown is not None else config.get("cooldown_seconds", 5)

    # Check INSTRUCTION.md exists
    if not os.path.exists(os.path.join(work_dir, "INSTRUCTION.md")):
        print("[giralph] No INSTRUCTION.md found. Run `giralph init` first.")
        sys.exit(1)

    # Check telegram before starting the loop
    tg_ok, tg_reason = check_telegram_configured()
    if not tg_ok:
        if not prompt_telegram_setup():
            print("[giralph] Set up Telegram first, then try again.")
            sys.exit(1)
        print("[giralph] Continuing without Telegram. Reports will only appear in HISTORY.md.\n")

    # Detect available agents
    available = detect_agents()
    print(f"[giralph] Detected agents:")
    print_agents(available)
    print()

    # Pick primary agent (CLI flag > config > auto-detect)
    preferred = args.agent or config.get("agent", "claude-code")
    agent = pick_agent(preferred, available)

    # Resolve debate agents — filter to available only
    raw_debate = args.debate.split(",") if args.debate else config.get("debate_agents", [])
    debate_agents = filter_debate_agents(raw_debate, available) if raw_debate else []

    # Judge must be available too
    judge = pick_agent(config.get("debate_judge", "claude-code"), available)

    print(f"[giralph] Starting loop")
    print(f"[giralph]   agent={agent}  work_dir={work_dir}")
    print(f"[giralph]   max_iterations={max_iter or 'infinite'}  cooldown={cooldown}s")
    if debate_agents:
        print(f"[giralph]   debate={debate_agents}  judge={judge}")
    print()

    # Circuit breaker state
    no_progress_count = 0

    iteration = 0
    while True:
        iteration += 1
        if max_iter and iteration > max_iter:
            print(f"[giralph] Reached max iterations ({max_iter}). Stopping.")
            break

        # Check state files exist
        if not os.path.exists(os.path.join(work_dir, "INSTRUCTION.md")):
            print(f"[giralph] No INSTRUCTION.md. Waiting {cooldown}s...")
            time.sleep(cooldown)
            continue

        # Snapshot file mtimes before running agent
        before = get_file_mtimes(work_dir)

        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[giralph] === Iteration {iteration} [{ts}] ===")

        # Run agent
        is_interactive = AGENTS[agent].get("interactive", False)

        if is_interactive:
            output = run_agent(agent, "", work_dir, iteration=iteration)
        elif debate_agents and len(debate_agents) > 1:
            full_prompt = build_prompt(
                read_file("INSTRUCTION.md", work_dir),
                read_file("MEMORY.md", work_dir),
                read_file("PLAN.md", work_dir),
                read_file("PROMPT.md", work_dir),
            )
            output, responses = debate(debate_agents, judge, full_prompt, work_dir)
        else:
            full_prompt = build_prompt(
                read_file("INSTRUCTION.md", work_dir),
                read_file("MEMORY.md", work_dir),
                read_file("PLAN.md", work_dir),
                read_file("PROMPT.md", work_dir),
            )
            output = run_agent(agent, full_prompt, work_dir, iteration=iteration)

        # Detect what files changed during this iteration
        after = get_file_mtimes(work_dir)
        files_changed = [f for f in after if before.get(f) != after.get(f)]

        agent_label = agent if not debate_agents else f"debate({','.join(debate_agents)})"
        log_iteration(iteration, agent_label, work_dir, files_changed, output)

        # === Circuit breaker evaluation ===
        if output == "interrupted":
            print(f"[giralph] User interrupted. Stopping.")
            break

        if is_interactive:
            # Interactive mode: check file changes for progress
            if files_changed:
                no_progress_count = 0
                print(f"[giralph]   Files updated: {', '.join(files_changed)}")
            else:
                no_progress_count += 1
                print(f"[giralph]   No file changes detected. no_progress={no_progress_count}")
                if no_progress_count >= 3:
                    print(f"[giralph] No progress for 3 iterations. Stopping.")
                    break
        else:
            # Headless mode: parse status block from stdout
            status = parse_status(output) if output else None
            if status:
                result = status.get("result", "").upper()
                exit_flag = status.get("exit", "").upper()
                exit_reason = status.get("exit_reason", "")

                print(f"[giralph]   status: result={result} exit={exit_flag}")

                if exit_flag == "YES":
                    print(f"[giralph] Agent requested exit: {exit_reason}")
                    break
                if result == "NO_WORK":
                    no_progress_count += 1
                    if no_progress_count >= 2:
                        print(f"[giralph] No work for {no_progress_count} iterations. Stopping.")
                        break
                else:
                    no_progress_count = 0
                if result == "BLOCKED":
                    print(f"[giralph] Agent blocked. Waiting {cooldown * 6}s...")
                    time.sleep(cooldown * 6)
                    continue
            elif not output:
                no_progress_count += 1
                if no_progress_count >= 3:
                    print(f"[giralph] No output for 3 iterations. Stopping.")
                    break

        print(f"[giralph] Cooling down {cooldown}s...\n")
        time.sleep(cooldown)

    print(f"[giralph] Loop ended after {iteration} iterations.")


def cmd_status(args):
    """Show current giralph state in this directory."""
    wd = get_work_dir()
    files = ["INSTRUCTION.md", "MEMORY.md", "PLAN.md", "PROMPT.md", "config.json", "HISTORY.md"]

    print(f"[giralph] Status for {wd}\n")
    for name in files:
        path = os.path.join(wd, name)
        if os.path.exists(path):
            size = os.path.getsize(path)
            print(f"  {name:20s} {size:>6d} bytes")
        else:
            print(f"  {name:20s} (missing)")

    # Show current task
    prompt = read_file("PROMPT.md", wd)
    if prompt.strip():
        print(f"\nCurrent task: {prompt.strip()[:100]}")
    else:
        print("\nNo task set. Edit PROMPT.md to add one.")

    # Show detected agents
    available = detect_agents()
    print(f"\nAgents:")
    print_agents(available)

    # Show telegram status
    tg_ok, tg_reason = check_telegram_configured()
    print(f"\nTelegram: {'configured' if tg_ok else tg_reason}")


# ── Main ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="giralph",
        description="giralph — open source ralph loop with multi-agent debate",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Scaffold giralph files into current directory")
    p_init.add_argument("-f", "--force", action="store_true", help="Overwrite existing files")

    # run
    p_run = sub.add_parser("run", help="Start the ralph loop")
    p_run.add_argument("-a", "--agent", default=None, help="Agent (auto-detects available, or specify one)")
    p_run.add_argument("-n", "--max-iterations", type=int, default=None, help="Max iterations (0=infinite)")
    p_run.add_argument("-c", "--cooldown", type=int, default=None, help="Seconds between iterations")
    p_run.add_argument("-w", "--work-dir", default=None, help="Working directory for agents")
    p_run.add_argument("-d", "--debate", default=None, help="Comma-separated agents for debate")

    # status
    sub.add_parser("status", help="Show giralph state in current directory")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        # Default: if INSTRUCTION.md exists, run; otherwise show help
        if os.path.exists(os.path.join(get_work_dir(), "INSTRUCTION.md")):
            args.agent = None
            args.max_iterations = None
            args.cooldown = None
            args.work_dir = None
            args.debate = None
            cmd_run(args)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
