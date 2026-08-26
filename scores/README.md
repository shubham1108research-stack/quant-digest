# Hand-produced rubric verdicts awaiting ingest.
#
# Written against the rubric in llm._SYSTEM and applied through
# llm._apply_score, so a paper judged here and one judged by the API differ
# only in who read the abstract. Every row is stamped with scored_by.
#
# Committed rather than passed as a workflow input: dispatch inputs cap at
# 64KB, and a few hundred verdicts pass that easily. It also means the
# archive scores arrive through a diff someone can read.
