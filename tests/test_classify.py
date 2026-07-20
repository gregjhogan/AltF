"""Classifier layering (DESIGN §11): structural facts first, LLM second,
verdicts cached per quiet period, failures degrade to working."""

import os
import pty as pty_module
import termios
import uuid
from types import SimpleNamespace

import pytest

from altf import LLMClassifier, Machine
from altf.console import Console


def make_console(**kwargs):
    return Console(
        name="c", slot="f1", purpose="test", write_fn=lambda data: None, **kwargs
    )


@pytest.fixture
def machine(tmp_path):
    return Machine(session=f"cls-{uuid.uuid4().hex[:6]}", workdir=tmp_path)


# ---------------------------------------------------------- layer 1: structure


async def test_alt_screen_is_awaiting(machine):
    console = make_console()
    console.alt_screen = True
    assert await machine._classify(console) == "awaiting"


async def test_echo_off_is_awaiting_when_termios_visible(machine):
    master, slave = pty_module.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        fake_pty = SimpleNamespace(fd=master)

        visible = make_console(pty=fake_pty, termios_visible=True)
        assert visible.echo_off
        assert await machine._classify(visible) == "awaiting"

        # a docker-style spawner's pty says nothing about the real terminal
        blind = make_console(pty=fake_pty, termios_visible=False)
        assert not blind.echo_off
        assert await machine._classify(blind) == "working"
    finally:
        os.close(master)
        os.close(slave)


async def test_no_hints_no_classifier_is_working(machine):
    assert await machine._classify(make_console()) == "working"


# --------------------------------------------------------------- layer 2: LLM


async def test_llm_verdict_propagates_and_is_cached_per_quiet_period(machine):
    calls = []

    async def model(prompt):
        calls.append(prompt)
        return "awaiting"

    machine.classifier = LLMClassifier(model)
    console = make_console()
    console.on_raw(10)
    assert await machine._classify(console) == "awaiting"
    assert await machine._classify(console) == "working"  # cached: same offset
    assert len(calls) == 1
    console.on_raw(5)  # new output -> new quiet period
    assert await machine._classify(console) == "awaiting"
    assert len(calls) == 2


async def test_llm_classifier_parses_wordy_replies():
    judge = LLMClassifier(lambda prompt: "I think it is AWAITING input.")
    assert await judge.classify("???", 5.0, "") == "awaiting"


async def test_llm_classifier_sees_tail_and_screen():
    seen = {}

    def model(prompt):
        seen["prompt"] = prompt
        return "working"

    judge = LLMClassifier(model)
    await judge.classify("$ make\ncompiling...", 3.5, "SCREEN TEXT")
    assert "compiling..." in seen["prompt"]
    assert "SCREEN TEXT" in seen["prompt"]
    assert "3.5" in seen["prompt"]


async def test_llm_classifier_garbage_and_errors_degrade_to_working():
    assert await LLMClassifier(lambda p: "no idea").classify("x", 5.0, "") == "working"

    def broken(prompt):
        raise RuntimeError("api down")

    assert await LLMClassifier(broken).classify("x", 5.0, "") == "working"
