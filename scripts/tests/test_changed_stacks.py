from scripts.changed_stacks import stacks_for


def test_stack_file_maps_to_stack():
    assert stacks_for(["infra/stacks/10-network/main.tf"]) == ["10-network"]


def test_module_change_maps_to_users():
    result = stacks_for(["infra/modules/network/main.tf"])
    assert "10-network" in result


def test_common_tfvars_touches_everything():
    result = stacks_for(["infra/envs/dev/common.tfvars"])
    assert "10-network" in result and "20-security" in result


def test_unrelated_paths_ignored():
    assert stacks_for(["README.md", "p1/generator/x.py"]) == []
