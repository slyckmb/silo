#!/usr/bin/env python3
"""Thin wrapper around hashall's shared qB cache agent."""

from qbit_hashall_shared import exec_hashall_script


if __name__ == "__main__":
    exec_hashall_script("qb-cache-agent.py")
