import pytest

from connectors.codex_bridge.article_queue import check_numbers


@pytest.mark.parametrize('claim,source', [
    ('R/S-5011/F8BT improved from 0.50 to 0.87.',
     'Organic chiral luminophores ( R / S -5011/F8BT) from 0.50 to 0.87.'),
    ('R / S -5011 / F8BT improved from 0.50 to 0.87.',
     'R/S-5011/F8BT improved from 0.50 to 0.87.'),
    ('R/S-5011/F8BT', 'R / S −5011/F8BT'),
])
def test_publisher_spacing_does_not_turn_material_identifier_into_negative_number(claim, source):
    check_numbers(claim, [source])


@pytest.mark.parametrize('claim,source', [
    ('R/S-5012/F8BT', 'R / S -5011/F8BT and 5012 samples.'),
    ('R/S-5011/F9BT', 'R / S -5011/F8BT'),
    ('R/T-5011/F8BT', 'R / S -5011/F8BT'),
    ('5011 samples', 'R / S -5011/F8BT'),
    ('-5011 samples', 'R / S -5011/F8BT'),
    ('R/S-5011/F8BT and 5011 samples', 'R / S -5011/F8BT'),
    ('The result was 5.', 'The result was -5.'),
    ('The result was -5.', 'The result was 5.'),
    ('A -5.7', 'A -5.3'),
    ('A -5%', 'A -5'),
    ('A -5', 'A -5%'),
    ('P -5e-4', 'P -5e-3'),
    ('P -5e+3', 'P -5e-3'),
    ('R/S-5011/F8BT improved to 0.97.', 'R / S -5011/F8BT improved to 0.87.'),
])
def test_identity_normalization_cannot_supply_or_change_numeric_evidence(claim, source):
    with pytest.raises(ValueError, match='关键数字'):
        check_numbers(claim, [source])


@pytest.mark.parametrize('claim,source', [
    ('R/S-5011/F8BT, 5 MPa.', 'R / S -5011/F8BT, 5 GPa.'),
    ('R/S-5011/F8BT, 5 MPa.', 'R / S -5011/F8BT, 5 mPa.'),
    ('R/S-5011/F8BT, -5 MPa.', 'R / S -5011/F8BT, 5 MPa; offset -5.'),
])
def test_identifier_formatting_does_not_relax_quantity_units(claim, source):
    with pytest.raises(ValueError, match='单位'):
        check_numbers(claim, [source])


def test_real_measurement_number_still_works_when_material_contains_same_digits():
    check_numbers('R/S-5011/F8BT, 5011 samples and -5 MPa.',
                  ['R / S -5011/F8BT, 5011 samples and -5 MPa.'])
