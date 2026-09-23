"""Settings of the learning engine (orchestrator/learning_engine.py), kept apart so config.py can import them."""

DEFAULTS = {
    "risk_check": True,                  # pre-flight risk and clarifying questions in the New task wizard
    "recommend_mode": "balanced",        # best | balanced | cheapest_good_enough
    "auto_pick_team": False,             # autopilot replaces the team of queued tasks that were not chosen by hand
    "explore_rate": 0.1,                 # share of auto-picks that try a less-proven alternative (never critical tasks)
    "lessons_selection": "relevant",     # relevant | all
    "auto_approve_lessons": False,       # approve proposed lessons backed by enough tasks (default: only mark them)
    "auto_approve_min_tasks": 3,
    "effect_min_tasks": 3,               # runs with a lesson before its effect gets a verdict
    "autopsy_score_threshold": 60,       # autopsy runs for failed tasks and those scoring below this
    "playbooks_inject": True,            # the repository's playbook goes into the supervisor's kickoff prompt
    "playbook_agent_refresh": True,      # a cheap agent turn refreshes playbooks with new evidence
    "playbook_refresh_hours": 24,
    "knowledge_inject": True,            # every kickoff prompt lists the task's knowledge docs (paths + one line each)
    "knowledge_refresh": True,           # daily: check repository docs against GitHub, refresh the stale ones (knowledge.py)
    "knowledge_refresh_hours": 24,
    "knowledge_min_commits": 10,         # commits since the doc's source commit that make it stale without key-file changes
    "knowledge_refresh_agent": "",       # blank: the retrospective agent (or the supervisor's) with its cheap model
    "knowledge_refresh_model": "",
    "knowledge_refresh_provider": "",    # "openrouter": run the refresh there (blank model = automatic best free model)
    "revert_window_days": 14,
    "repo_teams": {},                    # repository key → team key pinned from a proposal or by hand
}
