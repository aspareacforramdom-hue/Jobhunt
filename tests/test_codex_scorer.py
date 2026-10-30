import json
import copy
import pytest
from types import SimpleNamespace

from core import codex_scorer


def valid_rating():
    return {"url": "0", "breakdown": {"trajectory_fit": 8}, "qualification_gap": False,
            "qualification_gap_evidence": None, "verdict": "Relevant duties", "risk": None,
            "evidence": None, "angle": None, "stretch": False, "track": "both",
            "work_life_evidence": None}


def test_schema_requires_core_fit_and_allows_unknown_conditions():
    schema = codex_scorer._schema(["trajectory_fit", "people_process", "experience_fit", "compensation", "work_life_balance"])
    fields = schema["properties"]["scores"]["items"]["properties"]["breakdown"]["properties"]
    assert fields["trajectory_fit"]["type"] == "number"
    assert fields["people_process"]["type"] == "number"
    assert fields["experience_fit"]["type"] == "number"
    assert fields["compensation"]["type"] == ["number", "null"]
    assert fields["work_life_balance"]["type"] == ["number", "null"]


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), 0, 11, '8'])
def test_invalid_rating_is_not_published(value):
    row = valid_rating()
    row['breakdown']['trajectory_fit'] = value
    with pytest.raises(codex_scorer.CodexScoringError):
        codex_scorer._validate({'scores': [row]}, [{'url': '0'}], ['trajectory_fit'])


def test_duplicate_results_cannot_replace_missing_job():
    row = valid_rating()
    with pytest.raises(codex_scorer.CodexScoringError):
        codex_scorer._validate({'scores': [row, copy.deepcopy(row)]}, [{'url':'0'}, {'url':'1'}], ['trajectory_fit'])


def test_codex_never_inherits_service_credentials(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'test')
    monkeypatch.setenv('APIFY_TOKEN', 'test')
    env = codex_scorer._environment()
    assert not {'OPENAI_API_KEY','FIRECRAWL_API_KEY','APIFY_TOKEN'} & env.keys()
    assert any(k.upper() == 'PATH' for k in env)


def test_codex_uses_chatgpt_login_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(codex_scorer, "PRIVATE_DIR", tmp_path)
    monkeypatch.setattr(codex_scorer.shutil, "which", lambda name: "codex")

    def run(command, **kwargs):
        assert "--ephemeral" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[command.index("--model") + 1] == "gpt-6-luna"
        assert "Do not use tools" in kwargs["input"]
        schema_path = command[command.index("--output-schema") + 1]
        schema = json.loads(open(schema_path, encoding="utf-8").read())
        assert schema["properties"]["scores"]["items"]["properties"]["breakdown"]["required"] == ["trajectory_fit"]
        output_path = command[command.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump({
                "scores": [{
                    "url": "0",
                    "breakdown": {"trajectory_fit": 8.0},
                    "qualification_gap": False,
                    "stretch": False,
                    "track": "ideal",
                    "verdict": "Solid match",
                    "qualification_gap_evidence": None,
                    "risk": None,
                    "evidence": None,
                    "angle": None,
                    "work_life_evidence": None,
                }]
            }, handle)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_scorer.subprocess, "run", run)
    result = codex_scorer.score("rules", [{"url": "0"}], ["trajectory_fit"])
    assert result[0]["url"] == "0"
    assert result[0]["breakdown"] == {"trajectory_fit": 8.0}
    assert list(tmp_path.iterdir()) == []


def test_one_unknown_core_dimension_preserves_other_ratings(monkeypatch):
    from core import score
    profile = {"weights": {"trajectory_fit": 50, "experience_fit": 50}, "resume_text": "Bank operations"}
    jobs = [{"url": "https://example.com/" + str(i), "description": "Operations responsibilities", "evaluation": {}} for i in range(2)]
    def results(*args):
        rows = []
        for i in range(2):
            row = valid_rating()
            row["url"] = str(i)
            row["breakdown"] = {"trajectory_fit": 8, "experience_fit": 7 if i == 0 else None}
            rows.append(row)
        return rows
    monkeypatch.setattr(codex_scorer, "score", results)
    ratings = score._codex_score_jobs(jobs, profile)
    assert len(ratings) == 1
    assert ratings[0]["url"] == jobs[0]["url"]
    assert "score" not in jobs[1]
