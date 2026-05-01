What do I want to do with this application?


Send request -> API
    Authenticate as needed
API returns something
    Valid Protobuf
    Error Code
    Something else

We should ensure that anything other than a valid protobuf gets flagged appropriately

If Error Code: 
    Write to Parquet with Timestamp & what the error was
    Trigger DD alert

Something Else:
    I.e if HTML is returned (404 Page) write this error to parquet file

Valid Protobuf: 
    Save Protobuf and write out to parquet


Parquet file Schemas:
    Metadata Parquet: 
        This Parquet file should store data pertaining to timestamps, feeds, responses. 
        I.e I should be able to query this to ask questions like On this date how many requests had a 200 code?
        How many vehicles were in the feed for each request?

    
GTFS RT Parquets:
    These three will store the following information from the GTFS RT feeds. Feeds that mix the three types should be first unmixed then slotted into the appropriate Protobuf

    Vehicle Parquet:
        This schema will vary slightly depending on the protobuf used. 
        I.e Some agencies have extensions for fields that do not exist in others. I.e NYC MTA
    
    Service Alerts

    Trip Updates: 