# Databricks notebook source
# MAGIC %md
# MAGIC ## Add a Salt to Natural Key
# MAGIC To mitigate the damage that a hash table or a dictionary attack could do, we salt the passwords. According to the documentation, a salt is a value generated by a cryptographically secure function that is added to the input of hash functions to create unique hashes for every input, regardless of the input not being unique. A salt makes a hash function look non-deterministic, which is good as we don't want to reveal duplicate passwords through our hashing.
# MAGIC
# MAGIC Salting before hashing is very important as it makes dictionary attacks to reverse the hash much more expensive.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT *,sha2(string(deviceId),256) as sha2, sha2(concat(deviceId,'TEST'), 256) AS alt_id
# MAGIC FROM example.bronzeturbinet

# COMMAND ----------

# MAGIC %md
# MAGIC ## Register a SQL UDF
# MAGIC
# MAGIC Create a SQL user-defined function to register this logic to the current database under the name **`salted_hash`**. 
# MAGIC
# MAGIC This will allow this logic to be called by any user with appropriate permissions on this function. 

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE FUNCTION salted_hash (deviceId INT) RETURNS STRING
# MAGIC RETURN sha2(concat(deviceId,'TEST'),256)

# COMMAND ----------

# MAGIC %md
# MAGIC If your SQL UDF is defined correctly, the assert statement below should run without error.

# COMMAND ----------

# Check your work
salt = 'TEST'
set_a = spark.sql(f"SELECT sha2(concat('123', 'TEST'), 256)").collect()
set_b = spark.sql("SELECT salted_hash(123)").collect()
print(set_a)
print(set_b)
assert set_a == set_b, "The 'salted_hash' function is returning the wrong result."
print("All tests passed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Define a Function for Processing Incremental Batches
# MAGIC
# MAGIC Define a function to apply the SQL UDF registered above to create your **`alt_id`** to the **`deviceId`**.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT * FROM example.bronzeturbinet

# COMMAND ----------

(spark.readStream
        .table("example.bronzeturbinet")
        .selectExpr("salted_hash(deviceId) as new_id","deviceId")
        .writeStream
        .option("checkpointLocation", f"/FileStore/test")
        .trigger(availableNow=True)
        .table("example.test_target")
    )

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT * FROM example.test_target

# COMMAND ----------

# MAGIC %md
# MAGIC ## Deduplication with Windowed Functions
# MAGIC
# MAGIC We've previously explored some ways to remove duplicate records:
# MAGIC - Using Delta Lake's **`MERGE`** syntax, we can update or insert records based on keys, matching new records with previously loaded data
# MAGIC - **`dropDuplicates`** will remove exact duplicates within a table or incremental microbatch

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT * FROM example.bronzeturbinet

# COMMAND ----------

from pyspark.sql.window import Window
import pyspark.sql.functions as F

window = Window.partitionBy("deviceId").orderBy(F.col("timestamp").desc())

ranked_df = (spark.read.table("example.bronzeturbinet")
                      .withColumn("rank", F.rank().over(window))
                      .withColumn("rn", F.row_number().over(window))
                     .filter("rn == 1")
                     .drop("rn")
                     )
display(ranked_df)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Implementing Streaming Ranked/Row_number De-duplication
# MAGIC
# MAGIC As we saw previously, when apply **`MERGE`** logic with a Structured Streaming job, we need to use **`foreachBatch`** logic.
# MAGIC
# MAGIC Recall that while we're inside a streaming microbatch, we interact with our data using batch syntax.
# MAGIC
# MAGIC This means that if we can apply our ranked **`Window`** logic within our **`foreachBatch`** function, we can avoid the restriction throwing our error.

# COMMAND ----------

salt = "Test"
salted_df = (spark.readStream
                    .table("example.bronzeturbinet")
                    .filter("deviceId <> 0")
                    .select("*",F.sha2(F.concat(F.col("deviceId"), F.lit(salt)), 256).alias("alt_id")))

# COMMAND ----------

# MAGIC %md
# MAGIC The updated Window logic is provided below. Note that this is being applied to each **`micro_batch_df`** to result in a local **`ranked_df`** that will be used for merging.
# MAGIC  
# MAGIC For our **`MERGE`** statement, we need to:
# MAGIC - Match entries on our **`alt_id`**
# MAGIC - Update all when matched **if** the new record has is newer than the previous entry
# MAGIC - When not matched, insert all
# MAGIC
# MAGIC As before, use **`foreachBatch`** to apply merge operations in Structured Streaming.

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE TABLE IF NOT EXISTS  spark_catalog.example.bronzeturbinet_target (messageID STRING,deviceId INT,rpm DOUBLE,angle DOUBLE,timestamp TIMESTAMP,alt_id STRING)

# COMMAND ----------

from pyspark.sql.window import Window

window = Window.partitionBy("alt_id").orderBy(F.col("timestamp").desc())

def salted_upsert(microBatchDF, batchId):
    
    (microBatchDF
                 .withColumn("rank", F.rank().over(window))
                 .filter("rank == 1")
                 .drop("rank")
                 .createOrReplaceTempView("ranked_updates"))
    
    microBatchDF._jdf.sparkSession().sql("""
        MERGE INTO example.bronzeturbinet_target u
        USING ranked_updates r
        ON u.alt_id=r.alt_id
            WHEN MATCHED AND u.messageID < r.messageID
              THEN UPDATE SET *
            WHEN NOT MATCHED
              THEN INSERT *
    """)

# COMMAND ----------

query = (salted_df.writeStream
                    .foreachBatch(salted_upsert)
                    .outputMode("update")
                    .option("checkpointLocation", f"/FileStore/merge_salt")
                    .trigger(availableNow=True)
                    .start())

query.awaitTermination()

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT * FROM example.bronzeturbinet_target

# COMMAND ----------

# MAGIC %md
# MAGIC ## Dynamic Views
# MAGIC Views allow user or group identity ACLs to be applied to data at the column level.
# MAGIC
# MAGIC Database administrators can configure data access privileges to disallow access to a source table and only allow users to query a redacted view. 
# MAGIC
# MAGIC Users with sufficient privileges will be able to see all fields, while restricted users will be shown arbitrary results, as defined at view creation.

# COMMAND ----------

# MAGIC %md
# MAGIC We obfuscate the columns we want to be redacted

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE OR REPLACE VIEW redacted_view AS
# MAGIC   SELECT
# MAGIC     messageID,
# MAGIC     deviceId,
# MAGIC     CASE 
# MAGIC       WHEN is_member('test') THEN rpm
# MAGIC       ELSE 'REDACTED'
# MAGIC     END AS rpm,
# MAGIC     CASE 
# MAGIC       WHEN is_member('test') THEN angle
# MAGIC       ELSE 'REDACTED'
# MAGIC     END AS angle
# MAGIC   FROM example.bronzeturbinet

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT * FROM redacted_view

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT current_user();
