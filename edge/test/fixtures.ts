/** Response bodies shared by the local origin (Node) and the tests (workerd). */

/** Exact bytes the gzip and slow scenarios serve (formatting a JSON round trip would change). */
export const FIXED_BODY =
  '{"result":{"estimate":1.2345678901234567e-7,"n":42 },"note":"caf\\u00e9 é","x":1e400}'

/** The error scenario's API envelope. */
export const ERROR_BODY =
  '{"error":{"code":"validation_error","message":"The request does not match the API schema.",' +
  '"details":{"errors":[{"location":["body","years"],"message":"Field required","type":"missing"}]}}}'
