/**
 * CAREER LOOP — builds the intake form.
 *
 * WHAT TO DO
 *   1. Go to  script.google.com  and click  New project
 *   2. Delete whatever is in the editor, paste ALL of this in
 *   3. Click  Run  (the play button). Approve access when asked —
 *      it is your own account making your own form.
 *   4. The form's link is printed in the box at the bottom.
 *
 * Do not edit the question wording. Career Loop matches the answers
 * back to the questions by their text, so a changed word means that
 * answer is quietly dropped and the profile is built without it.
 */

var QUESTIONS = [
  {
    "id": "current_role",
    "ask": "What is your current job title, and how many years have you been working in total?",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "current_ctc",
    "ask": "What is your current CTC? This is only used to judge whether a job is actually a raise - it is never sent anywhere.",
    "help": "",
    "type": "number",
    "options": [],
    "track": ""
  },
  {
    "id": "location",
    "ask": "Which cities would you actually work in? Be honest about relocation, and say 'remote only' if that's the truth.",
    "help": "Answer in your own words.We have: Bangalore or fully remote. Change it if that's wrong.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "min_ctc",
    "ask": "Below what CTC would you say no, even to a great role?",
    "help": "We have: 25. Change it if that's wrong.",
    "type": "number",
    "options": [],
    "track": ""
  },
  {
    "id": "notice_reality",
    "ask": "How soon can you actually start?",
    "help": "We have: 2-3 months. Change it if that's wrong.",
    "type": "single",
    "options": [
      "Immediately",
      "1 month",
      "2-3 months",
      "Flexible"
    ],
    "track": ""
  },
  {
    "id": "ideal_role",
    "ask": "TRACK 1 - THE JOB YOU WANT. What role do you want to be doing in two years? Give the job title if you know it, or describe the work if you don't.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": "ideal"
  },
  {
    "id": "ideal_why",
    "ask": "What about that work appeals to you that your current job doesn't give you?",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": "ideal"
  },
  {
    "id": "ideal_gap",
    "ask": "What do you think is missing from your CV for that role right now? Answer honestly - this decides how far a stretch we reach for.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": "ideal"
  },
  {
    "id": "leverage_roles",
    "ask": "TRACK 2 - THE BEST-PAID JOB YOU COULD GET TODAY. Which job titles could you walk into right now with the experience you already have? List every one you would accept, even the boring ones.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": "leverage"
  },
  {
    "id": "leverage_floor",
    "ask": "For that kind of role, what salary would make it clearly worth moving for?",
    "help": "",
    "type": "number",
    "options": [],
    "track": "leverage"
  },
  {
    "id": "leverage_limits",
    "ask": "Is there any well-paid job you would refuse on principle, even for good money? Say so plainly.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": "leverage"
  },
  {
    "id": "track_balance",
    "ask": "Right now, which matters more?",
    "help": "",
    "type": "single",
    "options": [
      "The right job, money second",
      "Mostly the right job, but pay must not drop",
      "Even split - show me both",
      "Mostly the money, but not a dead end",
      "The money, clearly"
    ],
    "track": ""
  },
  {
    "id": "build_vs_operate",
    "ask": "Do you want to build new things, or improve existing systems?",
    "help": "",
    "type": "single",
    "options": [
      "Build from zero",
      "Mostly build",
      "Mostly improve",
      "Improve/optimise"
    ],
    "track": ""
  },
  {
    "id": "risk_appetite",
    "ask": "Early-stage chaos or established company structure?",
    "help": "",
    "type": "single",
    "options": [
      "Seed/Series A",
      "Series B-C",
      "Late stage",
      "Large enterprise"
    ],
    "track": ""
  },
  {
    "id": "growth_vs_comfort",
    "ask": "Would you take a harder role at lower pay if it moved you faster?",
    "help": "",
    "type": "single",
    "options": [
      "Yes, clearly",
      "Maybe, depends",
      "No, pay matters now"
    ],
    "track": ""
  },
  {
    "id": "domain_pull",
    "ask": "What subject matter would you genuinely enjoy working on daily? Name it in your own words.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "work_mode",
    "ask": "What's your real preference on office days?",
    "help": "",
    "type": "single",
    "options": [
      "Fully remote",
      "1-2 days office",
      "3-4 days office",
      "Don't care"
    ],
    "track": ""
  },
  {
    "id": "culture_dealbreaker",
    "ask": "What would make you quit within 6 months?",
    "help": "",
    "type": "multi",
    "options": [
      "Rigid hierarchy",
      "No autonomy",
      "Constant firefighting",
      "Formal dress code",
      "Heavy travel",
      "Long commute",
      "Night shifts / US hours",
      "None of these"
    ],
    "track": ""
  },
  {
    "id": "hard_no",
    "ask": "Anything else that is an absolute no? Night shifts, heavy travel, a specific company, a specific kind of team - name it.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "proof_points",
    "ask": "Name 2-3 things you have actually shipped, run or fixed. Real ones, with numbers if you have them - how many users, how much money, how much faster.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "unfair_advantage",
    "ask": "What can you say that 95% of other applicants cannot? It can be a skill, a domain, a language, a market you know, anything.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "why_now",
    "ask": "Why are you switching now, in one honest sentence?",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  }
];

function buildForm() {
  var form = FormApp.create('Career Loop — job search intake')
    .setDescription(
      'This builds a job search tuned to you.\n\n' +
      'There are two tracks, and they are scored separately:\n' +
      '  TRACK 1 — the job you want to grow into.\n' +
      '  TRACK 2 — the best-paid job your CV already qualifies you for.\n\n' +
      'Answer both honestly. Short answers are fine. The only wrong ' +
      'answer is a flattering one — this searches on what you write.')
    .setCollectEmail(false)
    .setProgressBar(true);

  QUESTIONS.forEach(function (q) {
    var item;
    if (q.type === 'single') {
      item = form.addMultipleChoiceItem();
      item.setChoiceValues(q.options);
    } else if (q.type === 'multi') {
      item = form.addCheckboxItem();
      item.setChoiceValues(q.options);
    } else if (q.type === 'number') {
      item = form.addTextItem();
    } else {
      item = form.addParagraphTextItem();
    }
    item.setTitle(q.ask);
    if (q.help) { item.setHelpText(q.help); }
    item.setRequired(false);
  });

  Logger.log('FORM (fill this in):  ' + form.getPublishedUrl());
  Logger.log('EDIT (yours to change): ' + form.getEditUrl());
}

function run() { buildForm(); }
