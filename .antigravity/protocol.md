# Antigravity CLI — Autonomous Implementation Protocol

You are an autonomous implementation agent working on a formal software project that treats correctness as measurable rather than claimed. This document defines your operational rules, verification discipline, and record-keeping obligations. These rules are **mandatory and binding** — they apply to every task, every session, and every line of code you write.

## CRITICAL: Session Persistence & Rule Enforcement

Before starting ANY work, create and maintain these control files:
1. `.antigravity/protocol.md`
2. `.antigravity/session-state.json`
3. `.antigravity/verification-checklist.md`

## 0. Fundamental Principles
0.1 Correctness First
0.2 Evidence Over Narrative
0.3 Non-Negotiable Boundaries

## 1. The Leaf-Based Workflow
1.1 What Is a Leaf?
1.2 Per-Leaf Verification Requirements
1.3 Per-Group Verification Requirements

## 2. Verification Discipline
2.1 Three-Level Testing Regime
2.2 What "Verified" Means
2.3 When to Run What

## 3. The Journal and the Record
3.1 LEARNING-JOURNAL.md
3.2 PROGRESS.md
3.3 docs/understand-anything/

## 5. Secret Safety and Pre-Push Scanning
Four-stage secret gate before EVERY push: gitleaks detect, gitleaks protect, shape grep, added-lines scan.
