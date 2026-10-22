"""DSH <-> Grok Bot bridge.

M1.5 task bus: dsh hands a task to grok through a GitHub issue, grok
replies with validated JSON. This package is transport-agnostic; see
``bridge.envelope`` for message validation.
"""
