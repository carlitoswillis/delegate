"""Who is actually talking in a delegated conversation.

The schema records `role` as "user" or "assistant" because that is what the
underlying APIs record. Rendering those two words directly is wrong here, and
wrong in a way that misleads rather than merely reading plainly.

In a delegated run nobody typed the "user" turn. It is a task file that
`delegate.sh` handed over, or a prompt Claude Code generated when it spawned a
subagent. Labelling it "user" tells you a human wrote it, and the whole point
of this viewer is reading back **what was actually asked** — a label that
attributes a machine-written brief to you is the same failure the README
describes, one layer up.

So the two sides get named after the parties that were really there: the
delegator, and the model that answered. Both names are already known — the
ledger records the model, and the platform tells you who did the delegating.
"""

from __future__ import annotations

# What a run's own platform implies about who handed the work over.
#
# A ledger run came from delegate.sh, which you invoked, so the sending side is
# you. A Claude Code subagent was spawned by a Claude Code session with no human
# in the loop at that moment, so the sender is that parent agent — saying "you"
# there would be a lie of exactly the kind this module exists to avoid.
_DELEGATOR = {
    "opencode": "you",
    "claude-code": "claude",
}


def short_model(model: str) -> str:
    """Drop the provider prefix: 'opencode/big-pickle' -> 'big-pickle'.

    The provider is already implied by which list the run came from, and the
    prefix eats the width that the actual model name needs.
    """
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def delegator_name(platform: str, *, is_subagent: bool = False) -> str:
    """Who sent the task. Never invents a human where there was not one."""
    if is_subagent:
        return "claude"
    return _DELEGATOR.get(platform, "delegator")


def speaker_for(role: str, *, model: str, platform: str,
                is_subagent: bool = False) -> str:
    """Display name for one turn's speaker.

    The "user" side is the delegator; the "assistant" side is the model that
    was delegated to, named concretely, because "assistant" is not a useful
    label in a list where several different models are answering.
    """
    if role == "user":
        return delegator_name(platform, is_subagent=is_subagent)
    name = short_model(model)
    return name or "agent"


def exchange_header(role: str, *, model: str, platform: str,
                    is_subagent: bool = False) -> str:
    """Header text for a block: the sender, and for a task also the receiver.

    The prompt side reads 'you → big-pickle' rather than just 'you', because
    the direction is the one fact a delegated transcript most needs to make
    obvious and the one a plain role label destroys.
    """
    speaker = speaker_for(role, model=model, platform=platform,
                          is_subagent=is_subagent)
    if role != "user":
        return speaker
    target = short_model(model) or "agent"
    return f"{speaker} → {target}"
