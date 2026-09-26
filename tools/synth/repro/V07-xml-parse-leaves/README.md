# V07-xml-parse-leaves - XML PARSE ... ON EXCEPTION GO TO opening a sentence is read as always leaving the paragraph

Found by the verifier of ROADMAP re-parse item 26, confirmed by the acceptance test. 1000-MAIN's only sentence is `XML PARSE WS-DOC PROCESSING PROCEDURE 3000-HANDLER ON EXCEPTION GO TO 9000-ERR END-XML.`; the statement splitter does not know XML, so the words before the GO TO - the ON EXCEPTION phrase - were lost and the GO TO read as one that always runs. Truth: 1000-MAIN falls through to 2000-CALLS when the parse succeeds. Tool: no fall-through edge, and `dead` says 2000-CALLS is reached by nothing.

Fixed in LESSONS 252: the words before the first verb the splitter knows are a statement of their own.
