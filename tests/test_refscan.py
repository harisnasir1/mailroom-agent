from matcher.refscan import normalise_ref, REF_PATTERN

def test_normalise_variants():
    for raw in ["SEN_A_102", "sen a 102", "SEN-A-102",
                "Sen.A.102", "SENA102", "sen_a_0102"]:
        assert normalise_ref(raw) == "sen_a_102", raw

def test_pattern_rejects():
    for bad in ["sen_b_102", "senator_102", "102", "sen_a_"]:
        assert not REF_PATTERN.search(bad), bad

def test_pattern_finds_in_context():
    assert REF_PATTERN.search("re: our ref Sen A 103, thanks")