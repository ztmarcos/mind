# Lambda and S3 triggers

## Summary
- S3 can invoke a Lambda function when new objects are added under a specified prefix
- This allows automating processing of new data in S3

## Key Concepts
- [[AWS Lambda]]
- [[AWS S3]]
- [[Event-driven architecture]]

## Insights
- Overlapping events: If multiple objects are added to the same S3 prefix, Lambda will be invoked for each new object
- Resource-based policy: A resource-based policy on the Lambda function may be required to grant S3 permission to invoke it

## Related
- [[Serverless computing]]
- [[Event-driven architecture patterns]]

## Sources
- No search results provided