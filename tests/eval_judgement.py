"""
JUDGEMENT ACCURACY — does the scorer actually believe what the product
claims to believe?

Every test in tests/ checks plumbing: does the CV reach the prompt, does
the cache engage, does a failed batch lose only its own eight. None of
them check the thing the product is actually FOR, which is an opinion:

    "A stretch role they can plausibly grow into scores HIGHER than a
     safe role that keeps them where they are."

That is a claim about judgement, and the only way to test it is to hand
the real model constructed cases where the right answer is known, and
see whether it agrees. Run against the live API, so it costs a few cents and tells the truth.

Deliberately NOT named test_*.py — pytest must not collect it, because a
suite that spends money every time it runs is a suite people stop
running.

    py -3.11 tests/eval_judgement.py

Last run, against gpt-6-sol: 8 of 8 assertions passed.
    stretch 8.7 · overreach 6.7 · lateral 6.4
    wrong_field 3.4 · backwards 3.1 · junk 1.0
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core import score                                      # noqa: E402

CV = """SREEHARI VILAS — Product Manager, Payments
Paytm — Senior Product Manager, Payments (2022–2026)
- Owned the reconciliation service processing 2M transactions per day
- Led the migration off a monolith to 14 services, cutting p99 latency 40%
- Grew UPI merchant activation 3x in eight months across 40k merchants
Kotak Mahindra — Product Manager (2019–2022)
- Shipped the first in-app lending flow: 400k applications in year one
- Built the KYC pipeline cutting onboarding from 5 days to 6 minutes
Skills: payments, UPI, settlement, reconciliation, SQL, experimentation
No people-management experience. No design background.
"""

PROFILE = {
    "target": {"role_family": "Product Management",
               "alt_titles": ["Platform Product Manager",
                              "Payments Product Manager"],
               "seniority": "Senior"},
    "hard_filters": {},
    "weights": {"trajectory_fit": 34, "decision_ownership": 28,
                "domain_depth": 18, "company_quality": 12,
                "experience_fit": 8},
    "dimension_definitions": {
        "decision_ownership": "Does this role own the call, or execute "
                              "someone else's decision?",
        "domain_depth": "How deep into payments/fintech does this go?"},
    "angles": ["Owns payments infrastructure at real scale"],
    "resume_text": CV,
}


def job(key, title, company, description):
    return {"url": f"https://eval/{key}", "title": title,
            "company": company, "location": "Bengaluru",
            "description": description}


CASES = [
    job("stretch", "Director of Product — Payments Platform", "Razorpay",
        "Own the payments platform roadmap end to end. You will set "
        "direction for reconciliation, settlement and merchant "
        "onboarding, and represent the platform to the exec team. "
        "8+ years product experience, some people management expected."),

    job("lateral", "Senior Product Manager — Payments", "PhonePe",
        "Own reconciliation and settlement for UPI. Work with "
        "engineering on the ledger. 5-7 years in payments product. "
        "Essentially the role you have today, at a similar company."),

    job("backwards", "Associate Product Manager — Payments", "Cred",
        "Support the senior PM on payments features. Write specs, run "
        "standups, maintain the backlog. 1-3 years experience."),

    job("wrong_field", "Senior Product Manager — Ad Tech Creative Suite",
        "InMobi",
        "Own the creative authoring tools for advertisers. Deep "
        "background in ad formats, creative workflows and design tooling "
        "required. No payments exposure needed."),

    job("junk", "Telecalling Executive — Loan Collections", "A Consultancy",
        "Call customers about overdue EMIs. Target-driven. Freshers "
        "welcome. Immediate joiners preferred."),

    job("overreach", "VP of Engineering — Payments", "Juspay",
        "Lead a 120-person engineering organisation. Own hiring, "
        "architecture and delivery across payments infrastructure. "
        "15+ years, of which 8 managing managers."),
]

EXPECTED = {
    "stretch": "should beat the lateral role — the whole thesis",
    "lateral": "safe, competent, not a step forward",
    "backwards": "must rank low — it is a demotion",
    "wrong_field": "PM title, wrong domain — the classic false positive",
    "junk": "must be near the floor",
    "overreach": "not plausible from an IC with no management experience",
}


def main():
    print("=" * 72)
    print("  JUDGEMENT EVAL — live model, constructed cases")
    print("=" * 72)
    scored = score.score_jobs([dict(c) for c in CASES], PROFILE)
    by_key = {j["url"].rsplit("/", 1)[1]: j for j in scored}

    print(f"\n  {'case':12} {'score':>6}   verdict")
    print("  " + "-" * 68)
    for j in scored:
        key = j["url"].rsplit("/", 1)[1]
        print(f"  {key:12} {j['score']:6.1f}   {(j['verdict'] or '')[:52]}")
        if j.get("evidence"):
            print(f"  {'':12} {'':6}   cv: {j['evidence'][:52]}")

    print("\n  " + "-" * 68)
    print("  ASSERTIONS THE PRODUCT'S OWN THESIS REQUIRES\n")

    checks = []

    def check(name, ok, detail):
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"         {detail}")

    g = by_key.get
    if g("stretch") and g("lateral"):
        check("a stretch role beats a lateral one",
              g("stretch")["score"] > g("lateral")["score"],
              f"stretch {g('stretch')['score']:.1f} vs "
              f"lateral {g('lateral')['score']:.1f}")

    if g("lateral") and g("backwards"):
        check("a demotion ranks below a lateral move",
              g("backwards")["score"] < g("lateral")["score"],
              f"backwards {g('backwards')['score']:.1f} vs "
              f"lateral {g('lateral')['score']:.1f}")

    if g("stretch") and g("wrong_field"):
        check("right domain beats right title, wrong domain",
              g("stretch")["score"] > g("wrong_field")["score"],
              f"stretch {g('stretch')['score']:.1f} vs "
              f"wrong_field {g('wrong_field')['score']:.1f}")

    if g("junk"):
        check("obvious junk lands near the floor",
              g("junk")["score"] <= 3.5,
              f"junk scored {g('junk')['score']:.1f} (want <= 3.5)")

    if g("overreach") and g("stretch"):
        check("an implausible leap ranks below a plausible stretch",
              g("overreach")["score"] < g("stretch")["score"],
              f"overreach {g('overreach')['score']:.1f} vs "
              f"stretch {g('stretch')['score']:.1f}")

    ev = [k for k in ("stretch", "lateral") if (g(k) or {}).get("evidence")]
    check("the CV is cited on roles it is relevant to",
          len(ev) == 2, f"evidence present on: {ev or 'none'}")

    if g("junk"):
        check("no evidence is invented for an irrelevant role",
              not g("junk").get("evidence"),
              f"junk evidence: {g('junk').get('evidence')!r}")

    if g("overreach"):
        check("the honest reason against is stated",
              bool(g("overreach").get("risk")),
              f"risk: {(g('overreach').get('risk') or '')[:56]}")

    print(f"\n  {'=' * 68}")
    print(f"  {sum(checks)} of {len(checks)} judgement assertions passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
