package toolformat

type OpenAIFOrmatter struct {
	GenericFormatter
}

func (f *OpenAIFOrmatter) ModelFamily() string {
	return "openai"
}
