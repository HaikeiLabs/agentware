package toolformat

type DeepSeekFormatter struct {
	GenericFormatter
}

func (f *DeepSeekFormatter) ModelFamily() string {
	return "deepseek"
}
