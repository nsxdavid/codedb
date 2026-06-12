"""httpx-streaming-json-iteration validator.

Spec: Add Response.iter_json() and Response.aiter_json().
 - Raise httpx.DecodingError unless Content-Type is application/json (or any
   application/*+json), application/ndjson / application/x-ndjson, or
   application/json-seq. Media-type matching case-insensitive; params allowed.
 - A `charset` param must name a valid codec else DecodingError. With no charset,
   decode JSON text by JSON encoding detection (UTF-8/16/32, incl UTF-8 BOM).
 - `+json` suffix only applies to `application/` (image/svg+json rejected).
 - application/json + application/*+json: parse exactly one JSON text after
   skipping leading whitespace and optional UTF-8 BOM. Top-level array -> yield
   each element; else yield the single value. Only whitespace allowed after.
   Empty/whitespace-only payloads are an error.
 - NDJSON: lines split on LF/CR/CRLF; ignore blank lines; each non-blank line is
   exactly one JSON text; UTF-8 BOM only allowed at start of first non-blank line.
 - json-seq: empty/whitespace-only after leading ws -> yield nothing; else first
   non-ws char must be RS (0x1e); each record begins RS, ends before next RS or
   end; strip at most one trailing LF then parse one JSON text. Empty records
   between two RS are ignored; a final record with no JSON text is an error.
 - Streaming responses: iter_json consumes + closes the stream; a second iteration
   raises httpx.StreamConsumed. In-memory responses: iteration is repeatable.
"""
import sys, json, traceback

TASK_ID = "httpx-streaming-json-iteration"
RS = b"\x1e"


def _streaming_body(chunks):
    def gen():
        for c in chunks:
            yield c
    return gen()


def main():
    results = {"task": TASK_ID, "checks": {}}
    C = results["checks"]

    # ---- import_httpx (MUST be first; return immediately on failure) ----
    try:
        import httpx
        from httpx import Response, DecodingError, StreamConsumed
        # The new public feature: Response.iter_json / aiter_json must exist.
        if not hasattr(Response, "iter_json") or not hasattr(Response, "aiter_json"):
            raise ImportError("Response.iter_json / aiter_json not present")
        C["import_httpx"] = True
    except Exception as e:
        C["import_httpx"] = False
        C["import_httpx_err"] = f"{type(e).__name__}: {e}"
        return results

    import asyncio

    def mk(content, ct=None):
        headers = {} if ct is None else {"Content-Type": ct}
        return httpx.Response(200, headers=headers, content=content)

    # ---- 1: application/json single object yields one value ----
    try:
        r = mk(b'{"a": 1, "b": 2}', "application/json")
        out = list(r.iter_json())
        C["json_single_object"] = out == [{"a": 1, "b": 2}]
    except Exception as e:
        C["json_single_object"] = False
        C["json_single_object_err"] = f"{type(e).__name__}: {e}"

    # ---- 2: application/json top-level array yields each element ----
    try:
        r = mk(b'[1, 2, {"x": 3}]', "application/json")
        out = list(r.iter_json())
        C["json_array_elements"] = out == [1, 2, {"x": 3}]
    except Exception as e:
        C["json_array_elements"] = False
        C["json_array_elements_err"] = f"{type(e).__name__}: {e}"

    # ---- 3: application/*+json suffix accepted; params + case-insensitive ----
    try:
        r = mk(b'{"ok": true}', "Application/Vnd.Api+JSON; charset=utf-8")
        out = list(r.iter_json())
        C["json_suffix_and_params"] = out == [{"ok": True}]
    except Exception as e:
        C["json_suffix_and_params"] = False
        C["json_suffix_and_params_err"] = f"{type(e).__name__}: {e}"

    # ---- 4: wrong content-type (text/plain) raises DecodingError ----
    try:
        r = mk(b'{"a": 1}', "text/plain")
        try:
            list(r.iter_json())
            C["reject_text_plain"] = False
        except DecodingError:
            C["reject_text_plain"] = True
    except Exception as e:
        C["reject_text_plain"] = False
        C["reject_text_plain_err"] = f"{type(e).__name__}: {e}"

    # ---- 5: +json suffix only on application/ tree (image/svg+json rejected) ----
    try:
        r = mk(b'{"a": 1}', "image/svg+json")
        try:
            list(r.iter_json())
            C["reject_non_application_suffix"] = False
        except DecodingError:
            C["reject_non_application_suffix"] = True
    except Exception as e:
        C["reject_non_application_suffix"] = False
        C["reject_non_application_suffix_err"] = f"{type(e).__name__}: {e}"

    # ---- 6: missing content-type raises DecodingError ----
    try:
        r = mk(b'{"a": 1}', None)
        try:
            list(r.iter_json())
            C["reject_missing_content_type"] = False
        except DecodingError:
            C["reject_missing_content_type"] = True
    except Exception as e:
        C["reject_missing_content_type"] = False
        C["reject_missing_content_type_err"] = f"{type(e).__name__}: {e}"

    # ---- 7: invalid charset param raises DecodingError ----
    try:
        r = mk(b'{"a": 1}', "application/json; charset=not-a-real-codec")
        try:
            list(r.iter_json())
            C["reject_invalid_charset"] = False
        except DecodingError:
            C["reject_invalid_charset"] = True
    except Exception as e:
        C["reject_invalid_charset"] = False
        C["reject_invalid_charset_err"] = f"{type(e).__name__}: {e}"

    # ---- 8: JSON encoding detection: UTF-16 + UTF-8 BOM, no charset given ----
    try:
        r16 = mk('{"u": "é"}'.encode("utf-16"), "application/json")
        out16 = list(r16.iter_json())
        bom = b"\xef\xbb\xbf" + b'{"u": 1}'
        rbom = mk(bom, "application/json")
        outbom = list(rbom.iter_json())
        C["json_encoding_detection"] = (
            out16 == [{"u": "é"}] and outbom == [{"u": 1}]
        )
    except Exception as e:
        C["json_encoding_detection"] = False
        C["json_encoding_detection_err"] = f"{type(e).__name__}: {e}"

    # ---- 9: leading whitespace + BOM skipped; trailing data is an error ----
    try:
        r_ok = mk(b'\xef\xbb\xbf   \n {"a": 1}  \n  ', "application/json")
        ok = list(r_ok.iter_json()) == [{"a": 1}]
        r_bad = mk(b'{"a": 1} {"b": 2}', "application/json")
        try:
            list(r_bad.iter_json())
            trailing_rejected = False
        except DecodingError:
            trailing_rejected = True
        C["json_whitespace_and_trailing"] = ok and trailing_rejected
    except Exception as e:
        C["json_whitespace_and_trailing"] = False
        C["json_whitespace_and_trailing_err"] = f"{type(e).__name__}: {e}"

    # ---- 10: empty/whitespace-only json payload is an error ----
    try:
        r = mk(b"   \n  ", "application/json")
        try:
            list(r.iter_json())
            C["json_empty_is_error"] = False
        except DecodingError:
            C["json_empty_is_error"] = True
    except Exception as e:
        C["json_empty_is_error"] = False
        C["json_empty_is_error_err"] = f"{type(e).__name__}: {e}"

    # ---- 11: NDJSON yields one value per non-blank line; blank lines ignored ----
    try:
        body = b'{"a": 1}\n\n  \n{"b": 2}\r\n{"c": 3}\r{"d": 4}'
        r = mk(body, "application/x-ndjson")
        out = list(r.iter_json())
        C["ndjson_lines"] = out == [
            {"a": 1}, {"b": 2}, {"c": 3}, {"d": 4},
        ]
    except Exception as e:
        C["ndjson_lines"] = False
        C["ndjson_lines_err"] = f"{type(e).__name__}: {e}"

    # ---- 12: NDJSON BOM allowed only at start of first non-blank line ----
    try:
        good = b"\xef\xbb\xbf" + b'{"a": 1}\n{"b": 2}'
        r_good = mk(good, "application/ndjson")
        good_ok = list(r_good.iter_json()) == [{"a": 1}, {"b": 2}]
        bad = b'{"a": 1}\n' + b"\xef\xbb\xbf" + b'{"b": 2}'
        r_bad = mk(bad, "application/ndjson")
        try:
            list(r_bad.iter_json())
            bad_rejected = False
        except DecodingError:
            bad_rejected = True
        C["ndjson_bom_rule"] = good_ok and bad_rejected
    except Exception as e:
        C["ndjson_bom_rule"] = False
        C["ndjson_bom_rule_err"] = f"{type(e).__name__}: {e}"

    # ---- 13: json-seq RS-delimited records; trailing LF stripped ----
    try:
        body = RS + b'{"a": 1}\n' + RS + b'{"b": 2}' + RS + b'3\n'
        r = mk(body, "application/json-seq")
        out = list(r.iter_json())
        C["json_seq_records"] = out == [{"a": 1}, {"b": 2}, 3]
    except Exception as e:
        C["json_seq_records"] = False
        C["json_seq_records_err"] = f"{type(e).__name__}: {e}"

    # ---- 14: json-seq empty/whitespace-only payload yields nothing ----
    try:
        r1 = mk(b"", "application/json-seq")
        empty_ok = list(r1.iter_json()) == []
        r2 = mk(b"   \n ", "application/json-seq")
        ws_ok = list(r2.iter_json()) == []
        C["json_seq_empty_yields_nothing"] = empty_ok and ws_ok
    except Exception as e:
        C["json_seq_empty_yields_nothing"] = False
        C["json_seq_empty_yields_nothing_err"] = f"{type(e).__name__}: {e}"

    # ---- 15: json-seq final record with no JSON text is an error ----
    try:
        # RS alone at end (no JSON text in final record) -> error
        body = RS + b'{"a": 1}\n' + RS
        r = mk(body, "application/json-seq")
        try:
            list(r.iter_json())
            C["json_seq_dangling_is_error"] = False
        except DecodingError:
            C["json_seq_dangling_is_error"] = True
    except Exception as e:
        C["json_seq_dangling_is_error"] = False
        C["json_seq_dangling_is_error_err"] = f"{type(e).__name__}: {e}"

    # ---- 16: streaming response: consumes stream; 2nd iteration -> StreamConsumed ----
    try:
        stream = _streaming_body([b'{"a":', b' 1}'])
        r = httpx.Response(
            200, headers={"Content-Type": "application/json"}, content=stream
        )
        out = list(r.iter_json())
        first_ok = out == [{"a": 1}]
        consumed_after = r.is_stream_consumed and r.is_closed
        try:
            list(r.iter_json())
            second_raised = False
        except StreamConsumed:
            second_raised = True
        C["streaming_consumes_and_closes"] = first_ok and consumed_after and second_raised
    except Exception as e:
        C["streaming_consumes_and_closes"] = False
        C["streaming_consumes_and_closes_err"] = f"{type(e).__name__}: {e}"

    # ---- 17: in-memory response: iter_json is repeatable ----
    try:
        r = mk(b'[1, 2, 3]', "application/json")
        first = list(r.iter_json())
        second = list(r.iter_json())
        C["in_memory_repeatable"] = first == [1, 2, 3] and second == [1, 2, 3]
    except Exception as e:
        C["in_memory_repeatable"] = False
        C["in_memory_repeatable_err"] = f"{type(e).__name__}: {e}"

    # ---- 18: aiter_json async iteration works ----
    try:
        async def run_async():
            r = httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'[10, 20]',
            )
            out = []
            async for v in r.aiter_json():
                out.append(v)
            return out
        out = asyncio.run(run_async())
        C["aiter_json_works"] = out == [10, 20]
    except Exception as e:
        C["aiter_json_works"] = False
        C["aiter_json_works_err"] = f"{type(e).__name__}: {e}"

    return results


if __name__ == "__main__":
    try:
        r = main()
    except Exception as e:
        r = {"task": TASK_ID, "fatal": str(e), "traceback": traceback.format_exc()}
    print(json.dumps(r, indent=2))
