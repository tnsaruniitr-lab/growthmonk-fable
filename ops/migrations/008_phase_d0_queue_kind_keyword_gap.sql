-- Phase D0 follow-up: allow keyword-gap detector rows in the operator queue.
-- Migration 003 pinned queue_items.kind to the four phase-C detectors; the D0
-- contract adds kind='keyword_gap' (gm/intel/labs.py) but migration 006 did not
-- extend the check constraint, so the insert would violate queue_items_kind_check.

alter table queue_items drop constraint queue_items_kind_check;
alter table queue_items add constraint queue_items_kind_check check (
  kind in ('striking_distance','decay','ctr_outlier','cannibalization','keyword_gap')
);
