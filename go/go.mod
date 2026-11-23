module github.com/haikeilabs/agentware/go

go 1.27

require (
	github.com/HaikeiLabs/kei-connector-contracts v0.10.8-0.20261024221321-dad7a39f22ed
	github.com/HaikeiLabs/kei-connector-contracts/gen/go v0.0.0-20261119143112-89917e5cb9ca
	github.com/soypete/ontology-go v0.0.0-20260730193222-0c5ba6ae2cd5
	google.golang.org/protobuf v1.36.10
	gopkg.in/yaml.v3 v3.0.1
)

// Local build until registry publishing; the real version pin comes with it
// (see the operations PR body).
replace github.com/HaikeiLabs/kei-connector-contracts/gen/go => ../../../kei-connector-contracts-wt/ctmain2/gen/go
