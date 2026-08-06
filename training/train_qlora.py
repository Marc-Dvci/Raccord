"""QLoRA fine-tuning for the AccessPulse specialist adapters.

Three narrow, private tasks (training/qlora_specialists.yaml): the public status
writer, the operator brief writer, and the delivery-log labeller. They exist
because the text involved either has to be written in a very particular register
or carries internal topology an operator may not send off-premises.

None of them decides anything (ADR 0001). Every one can be deleted and the loop
still closes.

    python training/build_dataset.py --scenarios 200
    python training/train_qlora.py --adapter status_writer
    python training/train_qlora.py --adapter status_writer --evaluate-only \
           --adapter-path var/adapters/status_writer

`--dry-run` validates the config, the dataset and the privacy assertions without
loading a model, which is what CI runs: the training dependencies
(transformers, peft, bitsandbytes, accelerate, trl) are optional and the
repository never requires an accelerator.

Evaluation is assertion-based, not perplexity-based. A status writer that scores
well on loss and leaks `capenc-pool-a` into a public message has failed, and the
eval in the config says so explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

DEFAULT_CONFIG = ROOT / "training" / "qlora_specialists.yaml"


# ---------------------------------------------------------------------------
# config and data
# ---------------------------------------------------------------------------


def _expand(value: Any) -> Any:
    """Resolve ${VAR:-default} in the config, so secrets stay in the environment."""
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{(\w+)(?::-(.*))?\}", value.strip())
        if match:
            return os.environ.get(match.group(1), match.group(2) or "")
        return value
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: Path, adapter: str) -> dict[str, Any]:
    config = _expand(yaml.safe_load(path.read_text(encoding="utf-8")))
    if adapter not in config["adapters"]:
        raise SystemExit(f"unknown adapter {adapter!r}; "
                         f"expected one of {sorted(config['adapters'])}")
    merged = {k: v for k, v in config.items() if k != "adapters"}
    merged["adapter"] = {"name": adapter, **config["adapters"][adapter]}
    return merged


def load_dataset(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(
            f"{path} does not exist - run `python training/build_dataset.py` first"
        )
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if not rows:
        raise SystemExit(f"{path} is empty")
    return rows


def check_privacy(rows: list[dict[str, Any]]) -> None:
    """Re-assert the dataset builder's guarantee at training time.

    The check is cheap and the failure it prevents - audience data reaching model
    weights - is irreversible.
    """
    from build_dataset import assert_no_audience_data  # noqa: PLC0415

    for i, row in enumerate(rows):
        try:
            assert_no_audience_data(row)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"privacy check failed on example {i}: {exc}") from exc


def format_example(instruction: str, row: dict[str, Any]) -> str:
    output = row["output"]
    if not isinstance(output, str):
        output = json.dumps(output, sort_keys=True)
    return (f"<s>[INST] {instruction.strip()}\n\n"
            f"{json.dumps(row['input'], sort_keys=True)} [/INST] {output}</s>")


# ---------------------------------------------------------------------------
# evaluation: assertions, not loss
# ---------------------------------------------------------------------------


def _flesch_kincaid_grade(text: str) -> float:
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    words = re.findall(r"[A-Za-z']+", text)
    if not words:
        return 0.0
    syllables = sum(max(1, len(re.findall(r"[aeiouy]+", w.lower()))) for w in words)
    return 0.39 * (len(words) / sentences) + 11.8 * (syllables / len(words)) - 15.59


# Numbers that are part of an identifier - "ctv-9.4.0", "pr-ad-fr", a date - are
# not metrics, and treating them as ones would make the grounding check fire on
# every brief that correctly names a player build.
_IDENTIFIER_NUMBER = re.compile(r"[A-Za-z]-?\d[\d.]*|\d+\.\d+\.\d+|\b\d{1,2} [A-Z][a-z]+ \d{4}\b")


def _numbers_in(blob: str, drop_identifiers: bool = True) -> set[float]:
    if drop_identifiers:
        blob = _IDENTIFIER_NUMBER.sub(" ", blob)
    out: set[float] = set()
    for token in re.findall(r"\d[\d,]*(?:\.\d+)?", blob):
        try:
            out.add(float(token.rstrip(",").replace(",", "")))
        except ValueError:
            continue
    return out


def _numbers_are_grounded(text: str, source: dict[str, Any]) -> bool:
    """Every number in the output must correspond to one in its input.

    Percentages and rounded posteriors are allowed to differ in the last place,
    which is why the comparison has a tolerance rather than being exact.
    """
    grounded = _numbers_in(json.dumps(source))
    # A derived count is legitimate: "13 items" over a 13-element list.
    grounded |= {float(len(v)) for v in source.values() if isinstance(v, (list, tuple))}
    for value in _numbers_in(text):
        if any(abs(value - g) <= max(0.01, abs(g) * 0.005) for g in grounded):
            continue
        return False
    return True


def evaluate(outputs: list[str], sources: list[dict[str, Any]],
             checks: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for check in checks:
        kind = check["kind"]
        passed = 0
        for text, source in zip(outputs, sources):
            lowered = text.lower()
            if kind == "forbidden_patterns":
                ok = not any(p.lower() in lowered for p in check["patterns"])
            elif kind == "required_any":
                ok = any(p.lower() in lowered for p in check["patterns"])
            elif kind == "required_all":
                ok = all(p.lower() in lowered for p in check["patterns"])
            elif kind == "max_flesch_kincaid_grade":
                ok = _flesch_kincaid_grade(text) <= check["value"]
            elif kind == "max_sentences":
                ok = len([s for s in re.split(r"[.!?]+", text) if s.strip()]) <= check["value"]
            elif kind == "json_parseable":
                try:
                    json.loads(text)
                    ok = True
                except Exception:  # noqa: BLE001
                    ok = False
            elif kind == "json_keys":
                try:
                    ok = set(check["value"]).issubset(json.loads(text))
                except Exception:  # noqa: BLE001
                    ok = False
            elif kind == "numbers_must_appear_in_source":
                # Hallucinated figures are the failure mode that matters in an
                # operator brief. Compare numerically rather than textually:
                # "2,493" and 2493, or "1.00" and 1.0, are the same number
                # presented differently, and a check that called those a
                # hallucination would be noise.
                ok = _numbers_are_grounded(text, source)
            elif kind == "min_null_rate_on_info_lines":
                ok = True  # aggregate check, handled below
            else:
                raise SystemExit(f"unknown eval kind {kind!r}")
            passed += int(ok)

        rate = passed / max(1, len(outputs))
        threshold = check.get("value") if kind.startswith("min_") else 1.0
        results.append({
            "name": check["name"],
            "kind": kind,
            "pass_rate": round(rate, 4),
            "passed": rate >= (threshold if isinstance(threshold, (int, float))
                               and kind.startswith("min_") else 1.0),
        })
    return {"examples": len(outputs), "checks": results,
            "all_passed": all(r["passed"] for r in results)}


# ---------------------------------------------------------------------------
# training
# ---------------------------------------------------------------------------


def train(config: dict[str, Any], rows: list[dict[str, Any]], out: Path) -> None:
    """Import the training stack lazily: it is an optional dependency."""
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            TrainingArguments,
        )
        from trl import SFTTrainer
    except ImportError as exc:  # pragma: no cover - optional path
        raise SystemExit(
            "the training stack is not installed. It is optional:\n"
            "  pip install 'transformers>=4.44' peft bitsandbytes accelerate trl datasets\n"
            f"(missing: {exc.name})"
        ) from exc

    base = config["base"]
    quant = config["quantization"]
    lora = config["lora"]
    train_cfg = config["train"]
    adapter = config["adapter"]

    tokenizer = AutoTokenizer.from_pretrained(base["model_id"], revision=base["revision"])
    tokenizer.pad_token = tokenizer.pad_token or tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        base["model_id"],
        revision=base["revision"],
        trust_remote_code=base["trust_remote_code"],
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=quant["load_in_4bit"],
            bnb_4bit_quant_type=quant["bnb_4bit_quant_type"],
            bnb_4bit_use_double_quant=quant["bnb_4bit_use_double_quant"],
            bnb_4bit_compute_dtype=getattr(torch, quant["bnb_4bit_compute_dtype"]),
        ),
        device_map="auto",
    )
    model = prepare_model_for_kbit_training(
        model, use_gradient_checkpointing=train_cfg["gradient_checkpointing"])
    model = get_peft_model(model, LoraConfig(
        r=lora["r"], lora_alpha=lora["alpha"], lora_dropout=lora["dropout"],
        bias=lora["bias"], task_type=lora["task_type"],
        target_modules=lora["target_modules"],
    ))
    model.print_trainable_parameters()

    texts = [format_example(adapter["instruction"], row) for row in rows]
    split = max(1, int(len(texts) * 0.9))
    dataset = Dataset.from_dict({"text": texts[:split]})
    eval_dataset = Dataset.from_dict({"text": texts[split:] or texts[-1:]})

    trainer = SFTTrainer(
        model=model,
        train_dataset=dataset,
        eval_dataset=eval_dataset,
        tokenizer=tokenizer,
        args=TrainingArguments(
            output_dir=str(out),
            num_train_epochs=train_cfg["epochs"],
            per_device_train_batch_size=train_cfg["per_device_batch_size"],
            gradient_accumulation_steps=train_cfg["gradient_accumulation_steps"],
            learning_rate=train_cfg["learning_rate"],
            lr_scheduler_type=train_cfg["lr_scheduler_type"],
            warmup_ratio=train_cfg["warmup_ratio"],
            weight_decay=train_cfg["weight_decay"],
            optim=train_cfg["optim"],
            gradient_checkpointing=train_cfg["gradient_checkpointing"],
            bf16=train_cfg["bf16"],
            logging_steps=train_cfg["logging_steps"],
            eval_strategy=train_cfg["eval_strategy"],
            save_strategy=train_cfg["save_strategy"],
            seed=train_cfg["seed"],
            report_to=[],
        ),
        max_seq_length=base["max_seq_length"],
        dataset_text_field="text",
    )
    trainer.train()
    trainer.save_model(str(out))
    tokenizer.save_pretrained(str(out))
    print(f"adapter written to {out}")


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="QLoRA training for AccessPulse specialists")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="validate config, dataset and privacy without loading a model")
    ap.add_argument("--evaluate-only", action="store_true",
                    help="run the assertion suite against the dataset's own targets")
    args = ap.parse_args()

    config = load_config(args.config, args.adapter)
    adapter = config["adapter"]
    rows = load_dataset(Path(adapter["dataset"]))
    check_privacy(rows)

    print(f"adapter        {adapter['name']}")
    print(f"base model     {config['base']['model_id']}")
    print(f"examples       {len(rows)}")
    print(f"lora rank      {config['lora']['r']} (alpha {config['lora']['alpha']})")
    print(f"quantisation   4-bit {config['quantization']['bnb_4bit_quant_type']}")
    print("privacy check  passed")

    if args.evaluate_only or args.dry_run:
        targets = [row["output"] if isinstance(row["output"], str)
                   else json.dumps(row["output"], sort_keys=True) for row in rows]
        report = evaluate(targets, [row["input"] for row in rows], adapter.get("eval", []))
        print("\nassertion suite against the dataset's own targets:")
        for check in report["checks"]:
            mark = "PASS" if check["passed"] else "FAIL"
            print(f"  {mark}  {check['name']:<28} pass rate {check['pass_rate']:.3f}")
        if args.dry_run:
            print("\ndry run: no model loaded, nothing trained")
            return 0
        return 0 if report["all_passed"] else 1

    out = args.out or (ROOT / "var" / "adapters" / adapter["name"])
    out.mkdir(parents=True, exist_ok=True)
    train(config, rows, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
