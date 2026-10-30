"""
STAGE 4a — the shortlister, with meaning instead of word overlap.

WHY THIS EXISTS

The prefilter decides which listings the scorer is allowed to read, and
it used to decide that by counting how many words a title shared with
the user's target. That is the crudest possible method and it has a
specific, expensive failure: "Associate Manager - Risk & Compliance"
shares two words with "Product Marketing Manager", so it ranked as a
strong match and got paid for. Meanwhile the budget it consumed was not
available for a role that was actually right.

Measured on this project's own vocabulary, against the target
"Brand Marketing Manager — owns creative direction for a consumer brand":

    0.66  Product Marketing Manager
    0.66  Brand Manager - Foods
    0.60  Assistant Brand Manager
    0.45  Marketing Manager (Growth/Performance)
    0.26  Associate Manager - Risk & Compliance     <- the false positive
    0.09  Telecalling Executive
   -0.03  Data Engineer - Pipelines

The ordering is the point. Word counting cannot produce it; this can.

WHY IT IS FREE

Static embeddings (model2vec). The model is ~30MB, runs on the CPU, and
took 1.1ms per listing here — 717 listings is under a second. No API, no
key, no per-call cost. This is the one place where spending nothing buys
better results rather than worse ones.

DEGRADES, NEVER BREAKS

If model2vec is not installed or the model has never been downloaded,
`available()` says so and the caller falls back to the old word-overlap
heuristic. A laptop with no internet still runs a scan; it just shortlists
less well.
"""

import os
import re

MODEL_NAME = os.environ.get("CAREERLOOP_EMBED_MODEL",
                            "minishlab/potion-base-8M")

# Loaded once per process and kept. Loading costs a second from a warm
# cache and ~25s the first time, when the model is downloaded.
_MODEL = None
_LOAD_FAILED = False


def available():
    """
    Can we embed without doing anything slow or surprising?

    Deliberately does NOT trigger a download — a scan should not stall
    for half a minute on its first run because of a shortlisting
    optimisation. Returns False until the model has been fetched, which
    `warm_up()` does explicitly.
    """
    if _MODEL is not None:
        return True
    if _LOAD_FAILED:
        return False
    try:
        import model2vec                                   # noqa: F401
    except ImportError:
        return False
    return _is_cached()


def _is_cached():
    """Has huggingface already got this model on disk?"""
    home = os.environ.get("HF_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface")
    slug = "models--" + MODEL_NAME.replace("/", "--")
    return os.path.isdir(os.path.join(home, "hub", slug))


def warm_up():
    """
    Download and load the model, blocking. Returns True on success.

    Call this from a setup command, not from a scan.
    """
    return _load() is not None


def _load():
    global _MODEL, _LOAD_FAILED
    if _MODEL is not None or _LOAD_FAILED:
        return _MODEL
    try:
        from model2vec import StaticModel
        _MODEL = StaticModel.from_pretrained(MODEL_NAME)
    except Exception as e:                                  # noqa: BLE001
        # Missing package, no network on a cold cache, corrupt download.
        # None of these should stop a scan.
        print(f"  [semantic] unavailable, falling back to keywords: {e}")
        _LOAD_FAILED = True
        return None
    return _MODEL


# ------------------------------------------------------------
# What we compare against
# ------------------------------------------------------------
def anchors(profile):
    """
    The target job titles, kept SEPARATE — one vector each.

    Blending them into a single averaged vector was measurably worse on
    this project's own data. "Brand & Product Marketing" + "Brand
    Manager" + "Product Marketing Manager" + "Consumer Marketing
    Manager" + "Brand Strategist" + "Marketing Manager" averages into a
    vector that mostly means "manager", and a manager is what it then
    finds:

        blended    0.62  Enterprise Account Manager - D2C Brands
                   0.62  Marketing Manager
                   0.59  Customer Loyality Manager
        per-term   0.81  Senior Product Marketing Manager
                   0.79  Principal Product Marketing Manager
                   0.79  Field Marketing Manager

    A person is not looking for the average of their target titles; they
    are looking for anything close to ANY of them. So each phrase gets
    its own vector and a listing scores on its best match.
    """
    profile = profile or {}
    parts = []
    # BOTH targets, because the shortlister decides what the scorer is
    # even allowed to look at. The intake infers two — the job she wants
    # to grow into, and the best-paid job her CV already sells — and a
    # leverage role that never clears the semantic floor is a role she
    # never sees, however well it pays. Leaving this on `target` alone
    # meant half the feature was collected, stored, and then filtered
    # out one step before it could matter.
    for key in ("target", "leverage_target"):
        block = profile.get(key) or {}
        parts.append(block.get("role_family") or "")
        parts += list(block.get("alt_titles") or [])
    parts += list(profile.get("search_terms") or [])

    seen, out = set(), []
    for p in parts:
        p = " ".join(str(p).split())
        key = p.lower()
        if p and key not in seen:
            seen.add(key)
            out.append(p)
    return out


_WS = re.compile(r"\s+")


def job_text(job):
    """
    Title and company. Deliberately NOT the description.

    Including even the first 400 characters of the description made
    shortlisting measurably worse — job descriptions open with the same
    corporate throat-clearing everywhere, so every listing drifts toward
    one bland centre and the title's signal is diluted:

        with description     0.62  Enterprise Account Manager - D2C Brands
                             0.61  Customer Loyality Manager
        title only           0.81  Senior Product Marketing Manager
                             0.79  Principal Product Marketing Manager

    The description is not wasted — it is what the scorer reads, later,
    on the survivors. This stage only has to decide who gets read.
    """
    bits = [job.get("title") or "", job.get("company") or ""]
    return _WS.sub(" ", ". ".join(b for b in bits if b)).strip()


# ------------------------------------------------------------
# Ranking
# ------------------------------------------------------------
def rank(jobs, profile):
    """
    Attach `_semantic_score` to every job, in place, and sort best-first.

    The score is the cosine similarity to the CLOSEST of the person's
    target titles, so a listing is judged on whether it looks like any
    one of the jobs they want, not like the average of all of them.

    Returns None when embedding is unavailable — None rather than an
    unsorted list, so the caller can tell "could not rank" apart from
    "ranked, and nothing matched".
    """
    if not jobs:
        return []
    if not available():
        return None
    model = _load()
    if model is None:
        return None

    targets = anchors(profile)
    if not targets:
        return None

    try:
        anchor_vecs = _unit(model.encode(targets))
        job_vecs = _unit(model.encode([job_text(j) for j in jobs]))
        # (jobs x dims) @ (dims x anchors) -> best anchor per job
        best = (job_vecs @ anchor_vecs.T).max(axis=1)
    except Exception as e:                                  # noqa: BLE001
        print(f"  [semantic] ranking failed, falling back: {e}")
        return None

    for job, score in zip(jobs, best, strict=True):
        job["_semantic_score"] = float(score)

    jobs.sort(key=lambda j: -j["_semantic_score"])
    return jobs


def _unit(vecs):
    """Row-wise L2 normalise, so a dot product is a cosine."""
    import numpy as np
    v = np.atleast_2d(np.asarray(vecs, dtype="float32"))
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.where(n == 0, 1.0, n)
