use agentware_kei::fixture::load_contract;

#[test]
fn loads_runtime_socket_constants_contract() {
    let contract = load_contract("runtime-socket/constants.json")
        .expect("runtime socket constants contract should load");

    assert_eq!(
        contract["socket"]["default_path"],
        "/run/kei-proxy/runtime.sock"
    );
}
