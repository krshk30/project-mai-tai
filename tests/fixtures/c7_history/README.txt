Three verbatim AST source fragments from locally retained historical commits.
manifest.json records each full original commit, source path, full-blob SHA256,
and extracted-fragment SHA256. The loader pins the manifest digest and fragment
bytes; the file's final newline is only a text-file terminator. No Git object,
network access, current-source substitution, or optional skip is used at test time.
The OMS method receives a class wrapper only for the unchanged AST selector.
The original mutation and independent literal assertions remain unchanged.
