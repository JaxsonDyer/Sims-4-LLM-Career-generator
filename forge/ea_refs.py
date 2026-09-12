"""
References into EA's base-game tuning that generated careers rely on.

A career cannot stand alone: going to work, the rabbit hole, promotion
buffs, notifications and work tones are all EA tuning that every career
points at by instance id. Everything here was read out of the game's own
combined tuning (SimulationDeltaBuild0.package, game version 1.127.41) and
checked to be:

  - base game. Nothing here needs a pack, so a generated career loads for
    any player. EA's own careers reference pack content in places; those
    references were left out rather than copied.
  - generic. Where EA has a per-career variant (the Writer's go-to-work
    interaction carries writing-themed work events), a neutral one was
    chosen instead, and every notification string was read to confirm it
    uses tokens ({0.SimFirstName}, {2.String}) rather than naming a career.

Pacing numbers (base performance, PTO) are identical across all EA adult
careers by level number and are copied as-is.
"""

from __future__ import annotations

GAME_VERSION = "1.127.41.1030"

# Career
CAREER_AFFORDANCE = 106449          # si_Career_Business: "Go to Work", no themed events
GO_HOME_TO_WORK_AFFORDANCE = 98770  # GoHomeAndAttendWork
CAREER_RABBIT_HOLE = 234354         # careerRabbitHole, shared by every EA career
PROMOTION_BUFF = (36045, 0x64105273)  # buff_Career_MovingUp, reason string
DEMOTION_BUFF = (36046, 0x72708373)   # buff_Career_RethinkingOptions
FIRED_BUFF = (97449, 0x9399F200)      # buff_Career_Fired

# Track
GOODBYE_NOTIFICATION = 110655       # notification_GoToWorkGoodbye_Generic
KNOWLEDGE_NOTIFICATION = 110279     # notification_AskAboutCareer_Generic
BUSY_TOOLTIP = 0x77D1D57E           # same on every EA career track
TRACK_IMAGE = 0x6665077284098FA2    # resource_icon_bkg.png

# Level
WORK_OUTFIT = 159412                # outfit_Uniform_Office_Worker
LOOT_ON_JOIN = [99354]              # Loot_Buff_Trait_NewJob_TraitActions
END_OF_DAY_LOOT = [163484, 277113, 283792, 324610]  # responsible trait, fears
PROMOTION_AUDIO = "39b2aa4a:00000000:c16f7f8c8e553337"
TONE_DEFAULT = 75577                # Career_Tone_Normal
TONE_LEAVE_EARLY = 100837           # CareerLeaveWorkEarly
TONES = [
    75577,   # Normal
    76717,   # Work Hard
    76759,   # Take It Easy
    76762,   # Socialize With Coworkers
    99731, 99743, 99724, 99720, 99739, 99725, 99746,  # emotional tones
    283822,  # fear confrontation: performance review
]

# EA's pacing by user level (1-based). Identical across Writer, TechGuru,
# Painter and the other adult careers.
BASE_PERFORMANCE = {1: 60, 2: 50, 3: 40, 4: 30, 5: 20, 6: 15, 7: 10, 8: 8, 9: 7, 10: 6}
PTO_PER_DAY = {1: 0.2, 2: 0.2125, 3: 0.225, 4: 0.2375, 5: 0.25,
               6: 0.2667, 7: 0.2833, 8: 0.3, 9: 0.3167, 10: 0.3333}
DAILY_ASSIGNMENT_PERFORMANCE = 30
MISSED_WORK_PENALTY = 15

# Career icons (PNG instance ids). Keyword -> (icon, high-res icon).
ICONS = {
    "freelancer": (0x522114564493C1F7, 0x5B175A6D0B1F38B9),
    "tech": (0x48703CA42B34B38F, 0xF7310ECB9DC346D1),
    "business": (0xC416BF0ABDB38640, 0xDC764A6B1247CE7C),
    "culinary": (0x334CFFE348815361, 0xE5EB210AB77CEA8B),
    "painter": (0xE3067D02F329DBF1, 0x23AA4DD9B156A0FB),
    "writer": (0x17B131133381DD3D, 0xCEC5E1B87E3367D7),
    "entertainer": (0xF73997FBC20207FF, 0xC51774A0DD047361),
    "athletic": (0x56CB427DA830E086, 0x2ACFC06D1FFC1EE2),
    "astronaut": (0x9D4FA16DB95C0AE5, 0x54BC338ADB3EF1BF),
    "criminal": (0x10FA66FC80563C0F, 0xDB7F82E445BE3851),
    "secret_agent": (0x3414AD50774CF2EF, 0x77DAE390EAF543B1),
    "style": (0x8CA3AC7B34F524AC, 0x5549530AA95158A8),
}
ICON_KEYWORDS = [
    ("tech", ("tech", "program", "software", "code", "coding", "computer", "ai ",
              "machine learning", "engineer", "data", "hacker", "developer")),
    ("business", ("business", "finance", "bank", "corporate", "manager", "sales",
                  "lawyer", "legal", "office", "consult")),
    ("culinary", ("chef", "cook", "culinary", "restaurant", "baker", "food")),
    ("painter", ("paint", "art", "artist", "design", "sculpt", "illustrat")),
    ("writer", ("writ", "author", "journal", "editor", "blog", "poet")),
    ("entertainer", ("music", "comed", "entertain", "actor", "perform", "sing", "dj")),
    ("athletic", ("athlet", "sport", "fitness", "coach", "trainer")),
    ("astronaut", ("space", "astronaut", "rocket", "scien", "research")),
    ("criminal", ("crime", "criminal", "thief", "villain", "heist")),
    ("secret_agent", ("spy", "agent", "detective", "police", "security")),
    ("style", ("fashion", "style", "influencer", "stream", "content creator", "social media")),
]


def pick_icon(*texts: str) -> tuple[int, int]:
    """Choose the closest EA career icon for a career's name / icon hint."""
    haystack = " ".join(texts).lower() + " "
    for key, words in ICON_KEYWORDS:
        if any(word in haystack for word in words):
            return ICONS[key]
    return ICONS["freelancer"]


# Base-game skill objectives, objective_Skill_<Skill>_Level<NN>, used as
# promotion gates. EA only ships objectives for some levels of some skills.
SKILL_OBJECTIVES: dict[str, dict[int, int]] = {
    "mixology": {2: 26403, 3: 26413, 4: 26415, 5: 26416, 7: 26429, 8: 26430, 10: 26432},
    "charisma": {2: 26463, 3: 26464, 4: 26465, 5: 26466, 6: 26467, 7: 26468, 8: 26470, 9: 202047, 10: 26472},
    "comedy": {2: 30448, 3: 30449, 6: 30463, 7: 30464, 8: 30465, 9: 30467, 10: 30468},
    "fishing": {2: 208525, 3: 208539, 4: 208540},
    "fitness": {2: 26580, 3: 26581, 4: 26582, 5: 26583, 6: 26584, 7: 26585, 8: 26586, 9: 26588, 10: 26589},
    "gardening": {2: 35182},
    "gourmet_cooking": {2: 26458, 4: 26456, 6: 26454, 8: 26449},
    "handiness": {2: 109075, 3: 218428, 4: 109078, 5: 218429, 6: 109079, 7: 218430, 8: 218421, 9: 218431, 10: 218423},
    "cooking": {2: 26425, 3: 26441, 4: 26442, 5: 26443, 6: 26444, 7: 26445, 8: 26446, 10: 26448},
    "logic": {2: 26603, 3: 26604, 4: 26606, 5: 26607, 6: 26608, 7: 204991, 8: 26611, 9: 204992, 10: 26613},
    "mischief": {2: 27427, 3: 27428, 4: 27429, 5: 27430, 6: 27431, 7: 27432, 8: 27433, 9: 27434, 10: 27435},
    "painting": {2: 33708, 3: 33709, 4: 33710, 5: 33711, 6: 33712, 7: 33713, 8: 33714, 9: 33715, 10: 33716},
    "photography": {2: 202032, 3: 202033, 4: 202031, 5: 202030},
    "piano": {2: 30632, 4: 30634, 6: 30636, 8: 30638, 10: 30640},
    "programming": {2: 218396, 3: 218398, 4: 218399, 5: 218400, 6: 218402, 7: 218403, 8: 218404, 9: 218405, 10: 218407},
    "rocket_science": {2: 109070, 4: 109071},
    "video_gaming": {2: 32811, 3: 32812, 4: 32814, 5: 32816, 6: 32817, 7: 32819, 8: 32820, 10: 32822},
    "writing": {2: 26592, 3: 26593, 4: 26594, 5: 26595, 6: 202027, 7: 26597, 8: 26598, 9: 26599, 10: 26600},
}


def skill_objective(skill: str, level: int) -> tuple[int, int] | None:
    """
    (objective id, level it actually checks) for a skill requirement.

    Uses EA's objective for that exact level, or the nearest level below it
    when EA has none. None when the skill has no base-game objective at or
    below the level (pack skills, level 1).
    """
    levels = SKILL_OBJECTIVES.get(skill.lower(), {})
    usable = [lvl for lvl in levels if lvl <= level]
    if not usable:
        return None
    best = max(usable)
    return levels[best], best


# ---------------------------------------------------------------------------
# XML blocks copied from career_Adult_Writer, with pack-only tests removed.
# ---------------------------------------------------------------------------

AVAILABILITY_TESTS = """
<L n="career_availablity_tests">
  <L><V t="sim_info"><U n="sim_info"><V t="specified" n="ages"><L n="specified">
    <E>YOUNGADULT</E><E>ADULT</E><E>ELDER</E>
  </L></V></U></V></L>
</L>"""

QUITTABLE_DATA = """
<V t="Can_Quit" n="quittable_data">
  <U n="Can_Quit">
    <U n="quit_dialog">
      <V t="enabled" n="icon"><V t="participant" n="enabled" /></V>
      <V n="text" t="single"><T n="single">0x6A9E0D87</T></V>
      <T n="text_ok">0x140935F4</T>
      <V n="title" t="enabled"><T n="enabled">0x36E52F06</T></V>
    </U>
  </U>
</V>"""


def _session_test(value: int) -> str:
    # statistic_Career_Session_Performance_Change, EA's generic work-day score
    return (
        '<L n="tests"><L><V t="statistic"><U n="statistic"><T n="stat">99886</T>'
        '<V t="value_threshold" n="threshold"><U n="value_threshold">'
        '<E n="comparison">GREATER_OR_EQUAL</E>'
        f'<T n="value">{value}</T></U></V></U></V></L></L>'
    )


def _tested_records(pairs: list[tuple[str, int | None]]) -> str:
    out = []
    for text, threshold in pairs:
        tests = _session_test(threshold) if threshold is not None else ""
        out.append(f'<U><U n="item"><V n="text" t="single"><T n="single">{text}</T></V></U>{tests}</U>')
    return "".join(out)


def _notification(name: str, text: str, title: str | None = None) -> str:
    title_xml = f'<V n="title" t="enabled"><T n="enabled">{title}</T></V>' if title else ""
    return f'<U n="{name}"><V n="text" t="single"><T n="single">{text}</T></V>{title_xml}</U>'


_BOSS_CALL_DIALOG = (
    '<U n="dialog"><V t="enabled" n="icon"><V t="resource_key" n="enabled"><U n="resource_key">'
    '<T n="key">2f7d0004:00000000:617140672fa22f7b</T></U></V></V>'
    '<E n="phone_ring_type">RING</E>'
    '<V n="text" t="single"><T n="single">0xA9E4B757</T></V>'
    '<V t="enabled" n="title"><T n="enabled">0x36795024</T></V></U>'
)


def _time_off(key: str, text: str | None = None, tooltip: str | None = None,
              day_end: str | None = None, disabled_day_end: bool = False) -> str:
    parts = []
    if day_end:
        parts.append(
            '<V t="enabled" n="day_end_notification"><V n="enabled" t="single"><U n="single">'
            f'<V n="text" t="single"><T n="single">{day_end}</T></V></U></V></V>'
        )
    elif disabled_day_end:
        parts.append('<V t="disabled" n="day_end_notification" />')
    if text:
        parts.append(f'<T n="text">{text}</T><T n="tooltip">{tooltip}</T>')
    value = f'<U n="value">{"".join(parts)}</U>' if parts else ""
    return f'<U><E n="key">{key}</E>{value}</U>'


CAREER_MESSAGES = (
    '<U n="career_messages">'
    '<V t="tested" n="career_assignment_summary_notification"><U n="tested"><L n="records">'
    + _tested_records([("0x208838E0", 60), ("0x1A4CAF4A", 20), ("0x47F9AD68", -20),
                       ("0xD46FA622", -60), ("0xEC8B05D7", None)])
    + '</L></U></V>'
    '<V t="tested" n="career_daily_end_notification"><U n="tested"><L n="records">'
    + _tested_records([("0x57D2BC57", 60), ("0xC721F09A", 20), ("0xB93A4CD6", -20),
                       ("0xF308EF2B", -60), ("0x84DB7BA6", None)])
    + '</L></U></V>'
    + _notification("career_daily_start_notification", "0x71EB46BD")
    + '<V t="enabled" n="career_early_warning_alarm"><U n="enabled">'
      '<T n="call_in_sick_text">0x9291A330</T>'
      '<U n="dialog"><V t="single" n="text"><T n="single">0xCF6C9354</T></V>'
      '<V t="enabled" n="timeout_duration"><T n="enabled">55</T></V></U>'
      '<T n="go_to_work_text">0x55B02A1F</T><T n="take_pto_text">0x17539AB0</T>'
      '<T n="work_from_home_text">0x72F88945</T></U></V>'
    + _notification("career_early_warning_notification", "0xD1C5E030")
    + '<U n="career_event_confirmation_dialog"><V n="text" t="single"><T n="single">0xE0D6C9</T></V></U>'
      '<V t="enabled" n="career_event_end_warning"><U n="enabled"><U n="notification">'
      '<V n="text" t="single"><T n="single">0xB7AEF346</T></V><E n="urgency">URGENT</E></U></U></V>'
      '<V t="enabled" n="career_missing_work"><U n="enabled"><T n="affordance">77302</T>'
    + _BOSS_CALL_DIALOG
    + '<L n="loot"><T>287561</T></L></U></V>'
      '<U n="career_performance_warning"><T n="affordance">77301</T>'
    + _BOSS_CALL_DIALOG
    + '<U n="threshold"><E n="comparison">LESS_OR_EQUAL</E><T n="value">-50</T></U></U>'
      '<L n="career_time_off_messages">'
    + _time_off("BEREAVEMENT", "0x7DB8165B", "0x7DB8165B", day_end="0xE5C6E75")
    + _time_off("FAKE_SICK", "0xE2103E00", "0xE2103E00", day_end="0x3AFB8BE9")
    + _time_off("FAMILY_LEAVE", "0x9C5E2B38", "0xD23FD32A", day_end="0xE5C6E75")
    + _time_off("HOLIDAY", "0x99DDA42A", "0xD48E9BB0", day_end="0x69DC6A39")
    + _time_off("MISSING_WORK", "0x6FC9D196", "0x11C913F4", disabled_day_end=True)
    + _time_off("PTO", "0x256650E9", "0xC7BF8E69", day_end="0xA4361A49")
    + _time_off("SICK", "0xE2103E00", "0xE2103E00")
    + _time_off("WORK_FROM_HOME")
    + _time_off("WORK_FROM_HOME_NOBLE")
    + '</L>'
    + _notification("demote_career_notification", "0x89568E38", "0xA13D761D")
    + _notification("fire_career_notification", "0xE0880E5D", "0x1FD4456D")
    + '<V t="enabled" n="join_career_notification"><U n="enabled">'
      '<V n="text" t="single"><T n="single">0xCC4DF263</T></V>'
      '<V t="enabled" n="title"><T n="enabled">0xF72C1089</T></V></U></V>'
    + _notification("lay_off_career_notification", "0xCB0EAA64")
    + _notification("overmax_notification", "0xDD2A8D4A", "0xD50C3F93")
    + _notification("overmax_rewardless_notification", "0xBA6D0721", "0xD50C3F93")
    + _notification("promote_career_notification", "0xD3C01ACE", "0x54E0293D")
    + _notification("promote_career_rewardless_notification", "0x6565040", "0x54E0293D")
    + '<T n="pto_gained_text">0xC7D41BCE</T>'
      '<U n="quit_career_notification"><V t="enabled" n="audio_sting"><U n="enabled">'
      '<T n="audio">39b2aa4a:00000000:949d002e3e205221</T></U></V>'
      '<V n="text" t="single"><T n="single">0xC55953D2</T></V>'
      '<V t="enabled" n="title"><T n="enabled">0xAE185370</T></V></U>'
      '<U n="situation_leave_confirmation"><U n="dialog">'
      '<V n="text" t="single"><T n="single">0xF29F397C</T></V>'
      '<T n="text_cancel">0xD8F1DC84</T><T n="text_ok">0x5C71E97E</T></U>'
      '<T n="take_pto_button_text">0xA0C11A0D</T></U>'
    '</U>'
)
