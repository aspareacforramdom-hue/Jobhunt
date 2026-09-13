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
    "ask": "First, the boring bit. What is your current job title, and how many years have you been working in total?",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "current_ctc",
    "ask": "What is your current CTC, roughly? Only used to work out whether a job is actually a raise. It never leaves this laptop.",
    "help": "",
    "type": "number",
    "options": [],
    "track": ""
  },
  {
    "id": "min_ctc",
    "ask": "What is the lowest salary you would say yes to, for a job you genuinely wanted?",
    "help": "We have: 25. Change it if that's wrong.",
    "type": "number",
    "options": [],
    "track": ""
  },
  {
    "id": "location",
    "ask": "Where would you actually work?",
    "help": "",
    "type": "multi",
    "options": [
      "Bangalore - office or hybrid is fine",
      "Fully remote, anywhere in India",
      "Bangalore only, and only if the commute is short",
      "I would relocate for the right job",
      "Remote only - I do not want to go to an office"
    ],
    "track": ""
  },
  {
    "id": "notice_reality",
    "ask": "Realistically, when could you start?",
    "help": "",
    "type": "single",
    "options": [
      "Right away",
      "In about a month",
      "In 2-3 months",
      "3 months or more",
      "I am in no hurry - the right job or nothing"
    ],
    "track": ""
  },
  {
    "id": "good_day",
    "ask": "Picture a workday that flew by and left you feeling good. What were you most likely doing?",
    "help": "",
    "type": "multi",
    "options": [
      "Solving a puzzle nobody else could crack",
      "Making something messy become clean and organised",
      "Building or designing something new from scratch",
      "Helping someone and seeing it land",
      "Digging through numbers until the answer appeared",
      "Convincing a room and winning them over",
      "Writing or explaining something really clearly",
      "Fixing a broken thing and making it run properly",
      "Planning something complicated so it all fit together",
      "Learning something completely new"
    ],
    "track": ""
  },
  {
    "id": "lose_track_of_time",
    "ask": "Which of these makes you lose track of time?",
    "help": "",
    "type": "multi",
    "options": [
      "A spreadsheet that finally balances",
      "A design or layout that finally looks right",
      "A conversation where you changed someone's mind",
      "Reading about something you find interesting",
      "Tidying, sorting or systemising something",
      "Making a process faster",
      "Teaching or explaining something to someone",
      "Hunting down why something went wrong"
    ],
    "track": ""
  },
  {
    "id": "hand_me_the_problem",
    "ask": "Two problems land on your desk. Which do you pick up first?",
    "help": "",
    "type": "single",
    "options": [
      "The one nobody understands yet",
      "The one that is urgent and clearly defined",
      "The one where people are unhappy and need sorting out",
      "The one where the numbers do not add up",
      "The one that needs something built",
      "The one that needs a decision nobody wants to make"
    ],
    "track": ""
  },
  {
    "id": "proud_of",
    "ask": "When you have done something at work you were quietly proud of, what did it usually look like?",
    "help": "",
    "type": "multi",
    "options": [
      "Something I built or set up that is still being used",
      "A mess I cleaned up",
      "A number that went up or down because of me",
      "A person or team I helped get better",
      "A decision I made that turned out right",
      "Something I shipped on time when it looked impossible",
      "Something I made much simpler than it was"
    ],
    "track": ""
  },
  {
    "id": "compliment",
    "ask": "Which compliment would mean the most to you?",
    "help": "",
    "type": "single",
    "options": [
      "\"Nobody thinks about this as carefully as you do\"",
      "\"You made this so much simpler\"",
      "\"I could not have done this without you\"",
      "\"You were right\"",
      "\"You built something that lasted\"",
      "\"You are the only one who actually understands this\"",
      "\"Everything runs better since you took over\""
    ],
    "track": ""
  },
  {
    "id": "structure_pref",
    "ask": "Which job would you rather have?",
    "help": "",
    "type": "single",
    "options": [
      "Clear goals and a clear process - I know what good looks like",
      "Clear goals, but I decide how",
      "Vague goals and total freedom - I will figure it out",
      "Honestly, it depends on the day"
    ],
    "track": ""
  },
  {
    "id": "pace_pref",
    "ask": "What pace suits you?",
    "help": "",
    "type": "single",
    "options": [
      "Steady and predictable, few surprises",
      "Mostly steady with the odd busy stretch",
      "Fast, changing, a bit chaotic - I like the buzz",
      "Intense bursts, then room to breathe"
    ],
    "track": ""
  },
  {
    "id": "people_pref",
    "ask": "How much of your day would you like to involve other people?",
    "help": "",
    "type": "single",
    "options": [
      "Mostly on my own, deep in the work",
      "Small team, same few people, quiet",
      "Lots of collaboration across teams",
      "Constantly with people - that is the fun part"
    ],
    "track": ""
  },
  {
    "id": "variety_pref",
    "ask": "Which sounds better?",
    "help": "",
    "type": "single",
    "options": [
      "Getting really, really good at one thing",
      "A few related things I know well",
      "Something different every few months",
      "No two days the same"
    ],
    "track": ""
  },
  {
    "id": "decide_how",
    "ask": "When you have to make a call, what do you reach for?",
    "help": "",
    "type": "single",
    "options": [
      "The data - show me the numbers",
      "My gut - I usually know",
      "Ask the people affected",
      "Find out what worked last time",
      "Think it through carefully alone, then decide"
    ],
    "track": ""
  },
  {
    "id": "ownership_pref",
    "ask": "Which would you rather be?",
    "help": "",
    "type": "single",
    "options": [
      "The person who owns one thing completely",
      "The person who makes a big machine run smoothly",
      "The expert everyone asks",
      "The person who decides what the team does next",
      "The person who quietly makes everyone else better"
    ],
    "track": ""
  },
  {
    "id": "drains_you",
    "ask": "Which of these would genuinely ruin your week? Tick as many as are true.",
    "help": "",
    "type": "multi",
    "options": [
      "Being interrupted all day",
      "Having to sell or chase targets",
      "Repetitive work with no thinking in it",
      "Office politics and managing upwards",
      "Being micromanaged",
      "No clear answer to what I am supposed to do",
      "Sitting in meetings all day",
      "Working alone with nobody to talk to",
      "Being responsible for something I cannot control",
      "Constant firefighting and emergencies",
      "Doing the same thing I did five years ago",
      "Late night calls with another country"
    ],
    "track": ""
  },
  {
    "id": "worst_boss",
    "ask": "Which kind of manager would make you quit?",
    "help": "",
    "type": "multi",
    "options": [
      "Checks everything I do",
      "Never available when I need a decision",
      "Changes their mind constantly",
      "Takes credit for my work",
      "Only cares about numbers, not people",
      "Avoids hard conversations",
      "Expects me online at all hours"
    ],
    "track": ""
  },
  {
    "id": "past_job_wrong",
    "ask": "Think of a job or a stretch at work you did not enjoy. What was actually wrong with it?",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "chasing",
    "ask": "What are you really hoping this next job gives you? Pick up to three.",
    "help": "",
    "type": "multi",
    "options": [
      "More money, plainly",
      "Work I find genuinely interesting",
      "A title that reflects what I actually do",
      "Learning something that makes me more valuable",
      "Stability and security",
      "Time and energy left over for my life",
      "Respect - being taken seriously",
      "Doing something that matters to someone",
      "A path to leading a team",
      "Getting out of where I am"
    ],
    "track": ""
  },
  {
    "id": "two_years",
    "ask": "It is two years from now and this move worked out beautifully. What is true?",
    "help": "",
    "type": "multi",
    "options": [
      "I am earning significantly more",
      "I am doing work I find genuinely interesting",
      "I am senior to where I am now",
      "I am known as good at something specific",
      "I have a team",
      "I finish at a reasonable hour and I am fine",
      "I changed field completely and it worked",
      "I have options - people want to hire me"
    ],
    "track": ""
  },
  {
    "id": "tradeoff",
    "ask": "A job offers everything you want but 20% less money. What do you do?",
    "help": "",
    "type": "single",
    "options": [
      "Take it - the work matters more",
      "Take it if the money catches up within a year",
      "Negotiate hard, then probably take it",
      "Probably not - money matters right now",
      "No - I am not going backwards on pay"
    ],
    "track": ""
  },
  {
    "id": "track_balance",
    "ask": "Two job lists. One has jobs you would love but would have to stretch for. The other has jobs you could get comfortably that pay well. Which do you want to see?",
    "help": "",
    "type": "single",
    "options": [
      "Mostly the stretch ones - aim high",
      "More stretch than safe, but show me both",
      "An even mix, I will judge each one",
      "More safe than stretch, but show me both",
      "Mostly the well-paid ones - I want a solid move"
    ],
    "track": ""
  },
  {
    "id": "ideal_direction",
    "ask": "TRACK 1, THE JOB YOU WANT. Forget titles and whether you are qualified. If you could spend your working day doing anything, which of these is closest?",
    "help": "",
    "type": "multi",
    "options": [
      "Building or creating things",
      "Analysing and finding out what is true",
      "Organising and making things run properly",
      "Helping and developing people",
      "Designing how something looks or feels",
      "Persuading, negotiating, winning business",
      "Writing, explaining, teaching",
      "Leading, deciding, being accountable",
      "Working with money, risk or rules",
      "Working closely with technology"
    ],
    "track": "ideal"
  },
  {
    "id": "ideal_freehand",
    "ask": "Anything you want to add about that? Even half an idea is useful - 'something in design but not graphic design', 'I keep thinking about HR', that sort of thing.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": "ideal"
  },
  {
    "id": "ideal_blocker",
    "ask": "What do you think is stopping you from getting that kind of job today?",
    "help": "",
    "type": "multi",
    "options": [
      "I do not have the right experience yet",
      "I do not have the right qualification or certificate",
      "Nobody would take me seriously for it",
      "I do not actually know what the job is called",
      "It would mean a pay cut",
      "Nothing really - I just have not looked properly",
      "I am not sure I am good enough"
    ],
    "track": "ideal"
  },
  {
    "id": "leverage_comfort",
    "ask": "TRACK 2, THE WELL-PAID JOB YOU COULD GET NOW. Someone offers you more money to do roughly what you already do. Your honest reaction?",
    "help": "",
    "type": "single",
    "options": [
      "Yes please, that is exactly what I want",
      "Happily, if the company and people are better",
      "Only if it is a lot more money",
      "Only as a stepping stone to something else",
      "No - I want to change what I do, not where I do it"
    ],
    "track": "leverage"
  },
  {
    "id": "leverage_roles",
    "ask": "Are there any job titles you already know you could do? List any that come to mind, even the dull ones. Leave it blank if you are not sure - we will work it out from your CV.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": "leverage"
  },
  {
    "id": "leverage_refuse",
    "ask": "Is there any well-paid job you would turn down on principle?",
    "help": "",
    "type": "multi",
    "options": [
      "Anything with sales targets",
      "Anything requiring night shifts or US hours",
      "Anything with heavy travel",
      "Anything back in a field I deliberately left",
      "Anything at a company I think behaves badly",
      "Nothing really - good money is good money"
    ],
    "track": "leverage"
  },
  {
    "id": "work_mode",
    "ask": "Office days - what do you actually want?",
    "help": "",
    "type": "single",
    "options": [
      "Fully remote",
      "1-2 days in office",
      "3-4 days in office",
      "Every day in office is fine",
      "I genuinely do not mind"
    ],
    "track": ""
  },
  {
    "id": "hard_no",
    "ask": "Anything that is an absolute no? A company, an industry, a kind of team, a commute, anything at all.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "come_to_you_for",
    "ask": "What do people at work always come to YOU for? However small, however odd.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "proof_points",
    "ask": "Name one or two things you have actually done at work that you would happily tell someone about. Numbers if you have them, but a plain sentence is fine.",
    "help": "Answer in your own words.",
    "type": "text",
    "options": [],
    "track": ""
  },
  {
    "id": "why_now",
    "ask": "Last one. Why are you looking now?",
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
