The application rejected your previous function call. Re-read the capability
instructions and source document, then make one corrected function call.

The rejection does not prove that the caller's input is incomplete. Call
`report_input_problem` only when the source itself lacks or conflicts on a
required fact. Otherwise call `submit_interpretation` again using only facts
supported by the source or the application-provided capability context, with
the exact required JSON types and fields.
