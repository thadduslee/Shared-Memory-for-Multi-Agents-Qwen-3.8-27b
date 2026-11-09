"""scripts/build_role_policy.py: prompts stay byte-identical for the archived domains; new domains only change words."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'data/gatemem'))
import build_role_policy as builder  # noqa: E402
from bench.domains import get_query_policy_block  # noqa: E402
from evaluation.gatemem_medical.hosts import HOSTS, policy_excluded_roles  # noqa: E402

FIXTURES = Path(__file__).with_name('fixtures')


def titles(domain):
    return builder.job_titles(ROOT / 'data/gatemem/bench/data' / domain / 'episodes.jsonl', set(policy_excluded_roles(domain)))


def test_medical_prompts_are_byte_identical_to_the_frozen_medical_policy_run():
    prov = json.loads((ROOT / 'runs/medical/role_policy.provenance.json').read_text())
    policy = json.loads((ROOT / 'runs/medical/role_policy.json').read_text())
    assert prov['policy_text'] == get_query_policy_block('medical')
    assert prov['job_titles'] == titles('medical')
    assert builder.step1_messages('medical', prov['policy_text'], prov['job_titles']) == prov['step1']['messages']
    frozen = {'role_descriptions': policy['role_descriptions'], 'information_kinds': policy['information_kinds']}
    assert builder.step2_messages('medical', prov['policy_text'], prov['job_titles'], frozen, False) == prov['step2']['messages']


def test_office_prompts_are_byte_identical_to_the_original_builder():
    fx = json.loads((FIXTURES / 'office_prompts_original_builder.json').read_text())
    assert fx['titles'] == titles('office')
    policy_text = get_query_policy_block('office')
    assert builder.step1_messages('office', policy_text, fx['titles']) == fx['step1']
    assert builder.step2_messages('office', policy_text, fx['titles'], fx['frozen'], True) == fx['step2']


def test_household_prompts_speak_of_a_household_and_its_helpers():
    frozen = {'role_descriptions': {t: 'x' for t in titles('household')}, 'information_kinds': {'k': 'y'}}
    text = json.dumps(builder.step1_messages('household', 'P', titles('household')) +
                      builder.step2_messages('household', 'P', titles('household'), frozen, False))
    import re
    assert not re.search(r'organisation|(?<![\w_])staff(?![\w_])', text)  # the title building_staff stays
    assert 'A household uses an AI assistant' in text and "the household's residents" in text
    assert 'A helper or service provider who has been shown doing work for a resident' in text


def test_education_authorities_are_asked_about_programs_and_projects_not_students():
    frozen = {'role_descriptions': {t: 'x' for t in titles('education')}, 'information_kinds': {'k': 'y'}}
    user = builder.step2_messages('education', 'P', titles('education'), frozen, True)[1]['content']
    assert "approve access to a campus program or research project's information" in user
    assert "organisation's students" in builder.step1_messages('education', 'P', titles('education'))[1]['content']


@pytest.mark.parametrize('domain', sorted(builder.DOMAINS))
def test_authorities_are_voted_on_exactly_for_hosts_whose_subjects_never_speak(domain):
    assert builder.DOMAINS[domain]['authorities'] == bool(HOSTS[domain].entity_kinds)


def test_default_exclusions_come_from_the_host():
    assert builder.default_excluded_roles('medical') == 'agent,family_member,patient'
    assert titles('education') == ['advisor', 'campus_it', 'club_officer', 'counselor', 'dean_assistant',
                                   'department_admin', 'financial_aid', 'professor', 'ra', 'registrar',
                                   'residence_staff', 'resident_assistant', 'ta']
    assert titles('household') == ['building_staff', 'caregiver', 'cleaner', 'driver_pickup_contact', 'home_it_helper',
                                   'household_manager', 'nanny', 'technician', 'trusted_contact']
