package backend

// defaultSeparator is used when joining multiple values (e.g. artist names,
// copyright notices) into a single string.
const defaultSeparator = ", "

// GetSeparator returns the default string used to join multi-value fields.
func GetSeparator() string {
	return defaultSeparator
}
