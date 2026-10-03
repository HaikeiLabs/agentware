package toolformat

type GLMFormatter struct {
	GenericFormatter
}

func (f *GLMFormatter) ModelFamily() string {
	return "glm"
}
