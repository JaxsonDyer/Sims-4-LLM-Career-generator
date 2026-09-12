"""
SimData for the tuning a career mod generates.

Schemas (names, hashes, field offsets) and table order are copied from EA's
own SimData for career_Adult_Writer and friends in game version 1.127, so
the UI finds every field where it expects it.
"""

from __future__ import annotations

from .ids import fnv32
from .schema import WEEKDAYS
from .simdata import (
    DataType as D, Field, Ref, RowBuilder, Schema, SimData, Table, raw_table,
    write_simdata,
)

# A VARIANT whose case is "disabled" is a null pointer tagged with this hash.
DISABLED = fnv32("disabled")


def _schema(name: str, schema_hash: int, size: int, fields: list[tuple[str, D, int]]) -> Schema:
    return Schema(name, schema_hash, size, [Field(n, t, o) for n, t, o in fields])


CAREER = _schema("Career", 0xC80851A2, 88, [
    ("build_buy_info", D.VARIANT, 0),
    ("call_costar_interaction", D.VARIANT, 8),
    ("cancel_audition_interaction", D.VARIANT, 16),
    ("cancel_gig_interaction", D.VARIANT, 24),
    ("career_category", D.INT64, 32),
    ("career_panel_type", D.INT64, 40),
    ("find_audition_interaction", D.VARIANT, 48),
    ("hire_agent_interaction", D.VARIANT, 56),
    ("reputation_stat", D.VARIANT, 64),
    ("show_ideal_mood", D.BOOL, 72),
    ("start_track", D.TABLESETREFERENCE, 80),
])

TRACK = _schema("TunableCareerTrack", 0x23557C02, 88, [
    ("branches", D.VECTOR, 0),
    ("busy_time_situation_picker_tooltip", D.LOCKEY, 8),
    ("career_description", D.LOCKEY, 12),
    ("career_levels", D.VECTOR, 16),
    ("career_name", D.LOCKEY, 24),
    ("career_name_gender_neutral", D.LOCKEY, 28),
    ("icon", D.RESOURCEKEY, 32),
    ("icon_high_res", D.RESOURCEKEY, 48),
    ("image", D.RESOURCEKEY, 64),
    ("show_now_hiring_string", D.BOOL, 80),
])

LEVEL = _schema("CareerLevel", 0x5FF24A48, 56, [
    ("agents_available", D.VECTOR, 0),
    ("aspiration", D.TABLESETREFERENCE, 8),
    ("ideal_mood", D.TABLESETREFERENCE, 16),
    ("pay_type", D.VARIANT, 24),
    ("performance_stat", D.TABLESETREFERENCE, 32),
    ("pto_per_day", D.FLOAT, 40),
    ("title", D.LOCKEY, 44),
    ("title_description", D.LOCKEY, 48),
    ("work_schedule", D.OBJECT, 52),
])
WEEKLY_SCHEDULE = _schema("TunableWeeklySchedule", 0xC897DDB0, 8, [
    ("schedule_entries", D.VECTOR, 0),
])
SCHEDULE_ENTRY = _schema("TunableScheduleEntry", 0xADFD3E3A, 48, [
    ("days_available", D.OBJECT, 0),
    ("duration", D.FLOAT, 4),
    ("multi_day_career_days_at_work", D.VARIANT, 8),
    ("multi_day_career_start_and_end_days", D.VARIANT, 16),
    ("random_start", D.BOOL, 24),
    ("schedule_shift_type", D.INT64, 32),
    ("start_time", D.OBJECT, 40),
])
AVAILABLE_DAYS = _schema("TunableAvailableDays", 0x5BCFCD54, 7, [
    (f"{i} {day.upper()}", D.BOOL, i) for i, day in enumerate(WEEKDAYS)
])
TIME_OF_DAY = _schema("TunableTimeOfDay", 0x1BD2886E, 8, [
    ("hour", D.INT32, 0),
    ("minute", D.INT32, 4),
])

STATISTIC = _schema("Statistic", 0x8273C673, 12, [
    ("max_value_tuning", D.INT32, 0),
    ("min_value_tuning", D.INT32, 4),
    ("stat_name", D.LOCKEY, 8),
])

ASPIRATION_CAREER = _schema("AspirationCareer", 0x8CAF3605, 8, [
    ("objectives", D.VECTOR, 0),
])

CAREER_CATEGORY_WORK = 1  # CareerCategory.Work
PAY_SIMOLEONS_PER_HOUR = fnv32("simoleons_per_hour")


def _main_table(name: str, schema: Schema) -> Table:
    return Table(name, schema, D.OBJECT, schema.size)


def career_simdata(name: str, start_track: int) -> bytes:
    table = _main_table(name, CAREER)
    row = RowBuilder(CAREER)
    for f in CAREER.fields:
        if f.type == D.VARIANT:
            row.set(f.name, (None, DISABLED))
    row.set("career_category", CAREER_CATEGORY_WORK)
    row.set("career_panel_type", 0)
    row.set("show_ideal_mood", False)
    row.set("start_track", start_track)
    row.add_to(table)
    return write_simdata(SimData([table]))


def track_simdata(name: str, *, career_name: int, career_name_neutral: int,
                  description: int, busy_tooltip: int, levels: list[int],
                  branches: list[int], icon: int, icon_high_res: int,
                  image: int, dds_type: int) -> bytes:
    table = _main_table(name, TRACK)
    # EA stores branches then levels in one shared table of tuning ids.
    ids = raw_table(D.TABLESETREFERENCE, branches + levels)
    row = RowBuilder(TRACK)
    row.set("branches", (Ref(ids, 0), len(branches)) if branches else None)
    row.set("career_levels", (Ref(ids, len(branches)), len(levels)))
    row.set("busy_time_situation_picker_tooltip", busy_tooltip)
    row.set("career_description", description)
    row.set("career_name", career_name)
    row.set("career_name_gender_neutral", career_name_neutral)
    row.set("icon", (dds_type, 0, icon))
    row.set("icon_high_res", (dds_type, 0, icon_high_res))
    row.set("image", (dds_type, 0, image))
    row.set("show_now_hiring_string", True)
    row.add_to(table)
    return write_simdata(SimData([table, ids]))


def level_simdata(name: str, *, title: int, title_description: int,
                  pay_per_hour: int, work_days: list[str], start_hour: int,
                  hours: float, performance_stat: int, aspiration: int,
                  pto_per_day: float, ideal_mood: int = 0) -> bytes:
    level = _main_table(name, LEVEL)
    weekly = _main_table("", WEEKLY_SCHEDULE)
    entry = _main_table("", SCHEDULE_ENTRY)
    days = _main_table("", AVAILABLE_DAYS)
    start = _main_table("", TIME_OF_DAY)
    pay = raw_table(D.INT32, [pay_per_hour])

    wanted = {d.lower() for d in work_days}
    day_row = RowBuilder(AVAILABLE_DAYS)
    for f in AVAILABLE_DAYS.fields:
        day_row.set(f.name, WEEKDAYS[f.offset] in wanted)
    days_ref = day_row.add_to(days)
    start_ref = RowBuilder(TIME_OF_DAY).set("hour", start_hour).set("minute", 0).add_to(start)

    entry_ref = (RowBuilder(SCHEDULE_ENTRY)
                 .set("days_available", days_ref)
                 .set("duration", float(hours))
                 .set("multi_day_career_days_at_work", (None, DISABLED))
                 .set("multi_day_career_start_and_end_days", (None, DISABLED))
                 .set("random_start", False)
                 .set("schedule_shift_type", 0)
                 .set("start_time", start_ref)
                 .add_to(entry))
    weekly_ref = RowBuilder(WEEKLY_SCHEDULE).set("schedule_entries", (entry_ref, 1)).add_to(weekly)

    (RowBuilder(LEVEL)
     .set("agents_available", None)
     .set("aspiration", aspiration)
     .set("ideal_mood", ideal_mood)
     .set("pay_type", (Ref(pay, 0), PAY_SIMOLEONS_PER_HOUR))
     .set("performance_stat", performance_stat)
     .set("pto_per_day", pto_per_day)
     .set("title", title)
     .set("title_description", title_description)
     .set("work_schedule", weekly_ref)
     .add_to(level))
    return write_simdata(SimData([level, weekly, entry, days, start, pay]))


def statistic_simdata(name: str, minimum: int, maximum: int) -> bytes:
    table = _main_table(name, STATISTIC)
    (RowBuilder(STATISTIC)
     .set("max_value_tuning", maximum)
     .set("min_value_tuning", minimum)
     .set("stat_name", 0)
     .add_to(table))
    return write_simdata(SimData([table]))


def aspiration_simdata(name: str, objectives: list[int]) -> bytes:
    table = _main_table(name, ASPIRATION_CAREER)
    ids = raw_table(D.TABLESETREFERENCE, objectives)
    RowBuilder(ASPIRATION_CAREER).set("objectives", (Ref(ids, 0), len(objectives))).add_to(table)
    return write_simdata(SimData([table, ids]))
