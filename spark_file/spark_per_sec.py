from pyspark.sql import SparkSession
from pyspark.sql.window import Window
from pyspark.sql.types import StructType, StructField, StringType, DoubleType, IntegerType, BooleanType, TimestampType
from pyspark.sql.functions import from_json, col, window, sum as spark_sum, count as spark_count,avg, last, lit, to_timestamp, current_timestamp
from pyspark.sql.functions import current_timestamp, window, when, lit, coalesce
from pyspark.sql import functions as SF
# from pyspark.sql.functions import udf


def main():
    schema = StructType([
        StructField("symbol", StringType(), True),
        StructField("type", StringType(), True),
        StructField("exchange", StringType(), True),
        StructField("market", StringType(), True),
        StructField("price", DoubleType(), True),
        StructField("size", IntegerType(), True),
        StructField("bid", DoubleType(), True),
        StructField("ask", DoubleType(), True),
        StructField("volume", IntegerType(), True),
        StructField("isContinuous", BooleanType(), True),
        StructField("time", StringType(), True), 
        StructField("serial", StringType(), True),
        StructField("id", StringType(), True),
        StructField("channel", StringType(), True),
        StructField("yesterday_price", DoubleType(), True),
    ])

    spark = SparkSession.builder \
        .appName("spark_per_sec_data") \
        .master("local[2]") \
        .config("spark.sql.streaming.pipelining.enabled", "true") \
        .config("spark.sql.streaming.pipelining.batchSize", "500") \
        .config("spark.sql.streaming.stateRebalancing.enabled", "true") \
        .config("spark.sql.streaming.offset.management.enabled", "true") \
        .config("spark.sql.streaming.stateStore.providerClass", "org.apache.spark.sql.execution.streaming.state.RocksDBStateStoreProvider") \
        .config("spark.sql.streaming.stateStore.rocksdb.changelogCheckpointing.enabled", "true") \
        .getOrCreate()
        # .config("spark.sql.streaming.stateStore.providerClass", "org.apache.spark.sql.execution.streaming.state.HDFSBackedStateStoreProvider") \
        # 確保有開啟 State Rebalancing 和 Enhanced Offset Management
        # 使用 pipelining
        # .config("spark.executor.cores", "2") \

    kafka_df = spark.readStream \
        .format("kafka") \
        .option("kafka.bootstrap.servers", "10.0.1.138:9092") \
        .option("subscribe", "kafka_raw_data") \
        .option("startingOffsets", "latest") \
        .option("maxOffsetsPerTrigger", "2000") \
        .option("failOnDataLoss", "false") \
        .load()
    
    def process_batch(df, epoch_id):
        try:
            df = df.selectExpr("CAST(value AS STRING) as json_data") \
                .select(from_json(col("json_data"), schema).alias("data")) \
                .select("data.*")

            df = df.withColumn("time", to_timestamp(col("time") / 1000000))
            windowed_df = df.groupBy(
                SF.window(col("time"), "1 second", "1 second"),col("symbol")
            ).agg(
                SF.sum(SF.when(col("type") != "heartbeat", col("price") * col("size")).otherwise(0)).alias("price_time_size"),
                SF.sum(SF.when(col("type") != "heartbeat", col("size")).otherwise(0)).alias("size_per_sec"),
                SF.last(SF.when(col("type") != "heartbeat", col("volume")).otherwise(0)).alias("volume_till_now"),
                SF.last("time", ignorenulls=True).alias("last_data_time"),
                SF.count(SF.when(col("type") != "heartbeat", col("symbol"))).alias("real_data_count"),
                SF.count(SF.when(col("type") == "heartbeat", col("symbol"))).alias("filled_data_count"),
                SF.last("yesterday_price", ignorenulls=True).alias("yesterday_price"),
            )#.orderBy("window.start")

            result_df = windowed_df.withColumn(
                "vwap_price_per_sec",
                SF.when(col("size_per_sec") != 0, col("price_time_size") / col("size_per_sec"))
                .otherwise(0)
            )
            result_df = result_df.withColumn(
                "price_change_percentage",
                SF.when(col("yesterday_price") != 0, 
                    SF.round(((col("vwap_price_per_sec") - col("yesterday_price")) / col("yesterday_price")) * 100, 2)
                ).otherwise(0)
            )
            result_df = result_df.withColumn(
                "real_or_filled", SF.when(col("real_data_count") > 0, "real").otherwise("filled")
            )

            result_df = result_df.orderBy("window.start") # 其實這個也沒有很消耗效能，昨天不知道為甚麼策出來很慢？？
            result_df.select(
                "symbol",
                SF.lit("per_sec_data").alias("type"),
                "window.start",
                "window.end",
                SF.current_timestamp().alias("current_time"),
                "last_data_time",
                "real_data_count",
                "filled_data_count",
                "real_or_filled",
                "vwap_price_per_sec",
                "size_per_sec",
                "volume_till_now",
                "yesterday_price",
                "price_change_percentage",
            ).selectExpr(
                "CAST(symbol AS STRING) AS key",
                "to_json(struct(*)) AS value"
            ).write \
                .format("kafka") \
                .option("kafka.bootstrap.servers", "10.0.1.138:9092") \
                .option("topic", "kafka_per_sec_data") \
                .save()
            
        except Exception as e:
            print(f"Error processing batch {epoch_id}: {e}")

    query = kafka_df.writeStream \
        .foreachBatch(lambda df, epoch_id: process_batch(df, epoch_id)) \
        .option("checkpointLocation", "/app/tmp/spark_checkpoints/spark_per_sec") \
        .start()
    # # # .trigger(processingTime='1 second') \理論上現在不應該用這個，因為這是每秒驅動一次，但如果資料累積，就會沒辦法每秒都運作，並且我已經有window來處理了
    query.awaitTermination()

if __name__ == "__main__":
    main()
        # b_vwap = -1
    # broadcast_vwap = spark.sparkContext.broadcast(b_vwap)

    # last_vwap_data = [None] #為了讓他可以
    # def update_vwap(current_vwap):
    #     if last_vwap_data[0] is None:
    #         last_value = None
    #     else:
    #         last_value = last_vwap_data[0]
    #     last_vwap_data[0] = current_vwap # if current_vwap != 0 else last_vwap_data[0]
    #     return last_value

    # update_vwap_udf = udf(update_vwap, DoubleType())

            # result_df = windowed_df.withColumn(
            #     "first_prev_vwap", update_vwap_udf(col("vwap_price_per_sec"))
            # )
            
            # current_broadcast_value = None if broadcast_vwap.value == -1 else broadcast_vwap.value
            # result_df = result_df.withColumn(
            #     "current_broadcast_value", SF.lit(current_broadcast_value)
            # )

            # result_df = result_df.withColumn(
            #     "vwap_price_per_sec",
            #     SF.when(
            #         col("vwap_price_per_sec") == 0,
            #         SF.coalesce(col("first_prev_vwap") ,col("yesterday_price"))  # , SF.lit(current_broadcast_value)
            #     ).otherwise(col("vwap_price_per_sec"))
            # )
    
            # last_non_zero_vwap = result_df.tail(1)[0]
            # if last_non_zero_vwap:
            #     broadcast_vwap.unpersist()
            #     broadcast_vwap = spark.sparkContext.broadcast(last_non_zero_vwap["vwap_price_per_sec"])
    
            # result_df = result_df.withColumn(
            #     "current_broadcast_value",
            #     SF.lit(current_broadcast_value)
            # )

            # window_spec = Window.partitionBy("symbol").orderBy("window.start")
            # result_df = windowed_df.withColumn(
            #     "prev_vwap", SF.expr("vwap_stateful(symbol, vwap_price_per_sec)")
            # )
            # result_df = windowed_df.withColumn(
            #     "prev_vwap", SF.lag("vwap_price_per_sec", 1).over(window_spec)
            # )

            # current_broadcast_value = broadcast_vwap.value


            # last_non_zero_vwap = result_df.filter(col("vwap_price_per_sec") != 0).select("vwap_price_per_sec").orderBy("window.end", ascending=False).first()
            # if last_non_zero_vwap:
            #     broadcast_vwap.unpersist()
            #     broadcast_vwap = spark.sparkContext.broadcast(last_non_zero_vwap["vwap_price_per_sec"])
            
            # current_broadcast_value = broadcast_vwap.value
            # windowed_df = windowed_df.withColumn(
            #     "initial_vwap",
            #     SF.when(col("size_per_sec") != 0, col("price_time_size") / col("size_per_sec")).otherwise(lit(current_broadcast_value))
            # )
            # result_df = windowed_df.withColumn(
            #     "prev_vwap", SF.lag("initial_vwap", 1).over(window_spec)
            # )
            # result_df = result_df.withColumn(
            #     "vwap_price_per_sec",
            #     SF.when(col("initial_vwap") == 0, SF.coalesce(col("prev_vwap"), SF.lit(current_broadcast_value)))
            #     .otherwise(col("initial_vwap"))
            # )

            # last_non_zero_vwap = result_df.filter(col("vwap_price_per_sec") != 0).select("vwap_price_per_sec").orderBy("window.end", ascending=False).first()
            # if last_non_zero_vwap:
            #     broadcast_vwap.unpersist()
            #     broadcast_vwap = spark.sparkContext.broadcast(last_non_zero_vwap["vwap_price_per_sec"])


 
            

# ---------------------------------------------------------------------------------------------------
            # 現在廣播是設定好了的
            # 現在想想，用這個廣好像沒什麼用意啊...因為他是在節點上運作...不會同步到另一個節點，也就是說B核心的spark沒辦法知道A核心中修改的值
            # 除非之後設定成昨天的收盤價格
            # ! 這邊的廣播設定有大問題，他會只存下非0的資料，這應該不對
            # 下方式重新負值的機制
            # last_non_zero_sma = result_df.filter(col("vwap_price_per_sec") != 0).select("vwap_price_per_sec").orderBy("window.end", ascending=False).first()
            # if last_non_zero_sma:
            #     broadcast_vwap.unpersist()
            #     broadcast_vwap = spark.sparkContext.broadcast(last_non_zero_sma["vwap_price_per_sec"])


            # current_broadcast_value = broadcast_vwap.value
            # windowed_df = windowed_df.withColumn(
            #     "initial_vwap",
            #     SF.when(col("size_per_sec") != 0, col("price_time_size") / col("size_per_sec")).otherwise(lit(current_broadcast_value))
            # )
            # result_df = windowed_df.withColumn(
            #     "prev_vwap", SF.lag("initial_vwap", 1).over(window_spec)
            # )
            # result_df = result_df.withColumn(
            #     "vwap_price_per_sec",
            #     SF.when(col("initial_vwap") == 0, SF.coalesce(col("prev_vwap"), SF.lit(current_broadcast_value)))
            #     .otherwise(col("initial_vwap"))
            # )

            # last_non_zero_sma = result_df.filter(col("vwap_price_per_sec") != 0).select("vwap_price_per_sec").orderBy("window.end", ascending=False).first()
            # if last_non_zero_sma:
            #     broadcast_vwap.unpersist()
            #     broadcast_vwap = spark.sparkContext.broadcast(last_non_zero_sma["vwap_price_per_sec"])
# ---------------------------------------------------------------------------------------------------

# 下面這方式是試圖在spark創造heartbeat，發現這方式完全不可行，浪費了一天
# schema = StructType([
#     StructField("symbol", StringType(), True),
#     StructField("type", StringType(), True),
#     StructField("exchange", StringType(), True),
#     StructField("market", StringType(), True),
#     StructField("price", DoubleType(), True),
#     StructField("size", IntegerType(), True),
#     StructField("bid", DoubleType(), True),
#     StructField("ask", DoubleType(), True),
#     StructField("volume", IntegerType(), True),
#     StructField("isContinuous", BooleanType(), True),
#     StructField("time", StringType(), True),
#     StructField("serial", StringType(), True),
#     StructField("id", StringType(), True),
#     StructField("channel", StringType(), True),
# ])


# def process_batch(df, epoch_id, spark):

#     if df.rdd.isEmpty():

#         filled_data = [("testing_partition",0.0,0.0,0.0,None,0,None,True)]
#         result_df = spark.createDataFrame(filled_data, schema)

#         # kafka_partition_value = df.select("kafka_partition").distinct().collect()
#         # print(f"kafka_partition_value: {kafka_partition_value}") 
#         # print(f"DataFrame schema: {df.schema}")

#         # partition_to_stock = {0:"2330",1:"0050",2:"00670L",3:"2454",4:"2603"}
#         # if kafka_partition_value:
#         #     kafka_partition = kafka_partition_value[0]["kafka_partition"]
#         #     symbol = partition_to_stock.get(int(kafka_partition), "UNKNOWN_PARTITION")
#         # else:
#         #     symbol = "UNKNOWN_PARTITION"


#     else:
#         df = df.withColumn("time", to_timestamp(col("time") / 1000000))
#         result_df = df.groupBy("symbol").agg(
#             last("symbol", ignorenulls=True).alias("symbol"),
#             sum(col("price") * col("size")).alias("price_time_size"),
#             sum("size").alias("size_per_sec"),
#             last("volume", ignorenulls=True).alias("volume_till_now"),
#             last("time", ignorenulls=True).alias("last_data_time"),
#             spark_count("*").alias("data_count"),
#         ).withColumn(
#             "vwap_price_per_sec",
#             when(col("size_per_sec") > 0, col("price_time_size") / col("size_per_sec")).otherwise(lit(None))
#         )
#         result_df = result_df.withColumn("is_filled", lit(False)) 

#     current_time = current_timestamp()
#     result_df = result_df.withColumn("current_time", lit(current_time))

#     result_df.selectExpr(
#         "CAST(symbol AS STRING) AS key",
#         "to_json(struct(*)) AS value"
#     ).write \
#         .format("kafka") \
#         .option("kafka.bootstrap.servers", "kafka:9092") \
#         .option("topic", "kafka_per_sec_data") \
#         .save()
#         # .partitionBy("symbol") \
#         # 這不管用，沒有辦法在spark來分配kafka的分區

# def main():
#     spark = SparkSession.builder \
#         .appName("spark_app_first") \
#         .config("spark.executor.cores", "2") \
#         .config("spark.cores.max", "2") \
#         .getOrCreate()

#     kafka_df = spark.readStream \
#         .format("kafka") \
#         .option("kafka.bootstrap.servers", "kafka:9092") \
#         .option("subscribe", "kafka_raw_data") \
#         .option("startingOffsets", "latest") \
#         .load()

#     kafka_df = kafka_df.selectExpr("CAST(value AS STRING) as json_data") \
#         .select(from_json(col("json_data"), schema).alias("data")) \
#         .select("data.*")
    
#     # kafka_df = kafka_df.withColumn("kafka_partition", col("partition"))
#     # kafka_df = kafka_df.selectExpr("CAST(value AS STRING) as json_data", "kafka_partition") \
#     #     .select(from_json(col("json_data"), schema).alias("data"), "kafka_partition") \
#     #     .select("data.*", "kafka_partition")

#     query = kafka_df.writeStream \
#         .trigger(processingTime='1 second') \
#         .foreachBatch(lambda df, epoch_id: process_batch(df, epoch_id, spark)) \
#         .start()

#     query.awaitTermination()

# if __name__ == "__main__":
#     main()

# ---------------------------------------------------------------------------------------------------
# def update_state(key, iterator, state: GroupState):
#     items = list(iterator)
#     symbol = partition_to_stock.get(int(key))
#     if items:
#         sum_price_time_size = sum(item.price * item.size for item in items)
#         size_per_sec = sum(item.size for item in items)
#         vwap_price_per_sec = sum_price_time_size / size_per_sec
#         volume_till_now = items[-1].volume
#         last_data_time = from_unixtime(items[-1].time / 1000000)
#         current_time = current_timestamp()
#         is_filled = False

#         state.update((symbol, vwap_price_per_sec, size_per_sec, volume_till_now, last_data_time, current_time, is_filled))

#     # # elif state.exists: # 這會儲存上一筆資料的值
#     # #     vwap_price_per_sec, size_per_sec, volume_till_now, last_data_time, current_time, is_filled = state.get()
#     # else:
#     #     add_filled_data()
#     #     # vwap_price_per_sec, size_per_sec, volume_till_now, last_data_time, current_time, is_filled= 0, 0, 0, None, current_timestamp(), True

#     return [(symbol, vwap_price_per_sec, size_per_sec, volume_till_now, last_data_time, current_time, is_filled)]


# ---------------------------------------------------------------------------------------------------

# def main():
#     spark = SparkSession.builder \
#         .appName("spark_app_first") \
#         .config("spark.executor.cores", "2") \
#         .config("spark.cores.max", "2") \
#         .getOrCreate()
#     # 這個設定可以確保在兩核心的前提下，大多數code可以保持員樣

#     schema = StructType([
#         StructField("symbol", StringType(), True),
#         StructField("type", StringType(), True),
#         StructField("exchange", StringType(), True),
#         StructField("market", StringType(), True),
#         StructField("price", DoubleType(), True),
#         StructField("size", IntegerType(), True),
#         StructField("bid", DoubleType(), True),
#         StructField("ask", DoubleType(), True),
#         StructField("volume", IntegerType(), True),
#         StructField("isContinuous", BooleanType(), True),
#         StructField("time", StringType(), True), 
#         StructField("serial", StringType(), True),
#         StructField("id", StringType(), True),
#         StructField("channel", StringType(), True)
#     ])
        
#     kafka_df = spark.readStream \
#         .format("kafka") \
#         .option("kafka.bootstrap.servers", "kafka:9092") \
#         .option("subscribe", "kafka_raw_data") \
#         .option("startingOffsets", "latest") \
#         .option("maxOffsetsPerTrigger", "1000") \
#         .load()

#     def process_batch(df, epoch_id):
#         if df.rdd.isEmpty():
#             print(f"Batch {epoch_id} is empty, skipping processing.")
#             return

#         df = df.selectExpr("CAST(value AS STRING) as json_data") \
#             .select(from_json(col("json_data"), schema).alias("data")) \
#             .select("data.*")
        
#         # 窗口的開啟基於我輸入的資料col("time")
#         df = df.withColumn("time", to_timestamp(col("time") / 1000000))
#         windowed_df = df.groupBy(
#             window(col("time"), "1 second"),
#             col("symbol")
#         ).agg(
#             spark_sum(col("price") * col("size")).alias("price_time_size"),  
#             spark_sum("size").alias("size_per_sec"),
#             last("volume", ignorenulls=True).alias("volume_till_now"),
#             last("time", ignorenulls=True).alias("last_data_time"),
#             last("isContinuous", ignorenulls=True).alias("isContinuous")
#         )

#         # 計算每秒資料，並且調整欄位名稱，準備輸出
#         result_df = windowed_df.withColumn(
#             "vwap_price_per_sec", col("price_time_size") / col("size_per_sec")
#         ).select(
#             "symbol",
#             "vwap_price_per_sec",
#             "size_per_sec",
#             "volume_till_now",
#             "last_data_time",
#             "isContinuous",
#             "window.start", 
#             "window.end",
#             current_timestamp().alias("current_time") 
#             # "last_serial as serial",  # 這個架構好像不支持這樣重新命名的操作
#             # col("last_isClose").alias("isClose"),  # 不然就是要這樣命名
#         )

#         result_df.selectExpr(
#             "CAST(symbol AS STRING) AS key",
#             "to_json(struct(*)) AS value"
#         ).write \
#             .format("kafka") \
#             .option("kafka.bootstrap.servers", "kafka:9092") \
#             .option("topic", "kafka_per_sec_data") \
#             .save()
            
#     query = kafka_df.writeStream \
#         .foreachBatch(process_batch) \
#         .trigger(processingTime='1 second') \
#         .option("checkpointLocation", "/app/tmp/spark_checkpoints") \
#         .start()
#     query.awaitTermination()

# if __name__ == "__main__":
#     main()

# ---------------------------------------------------------------------------------------------------

# class SparkHandler:
#     def __init__(self):
#         self.spark = SparkSession.builder \
#             .master("local[*]") \
#             .appName("SparkApp") \
#             .config("spark.executor.cores", "2") \
#             .getOrCreate()
        
#             # .config("spark.executor.instances", "1") \
#             # executor，是設立spark的「節點」，要在多台主機的情況這設置才有意義
#             # core，會用到兩個核心
#             # .config("spark.jars", "/opt/spark/jars/spark-sql-kafka-0-10_2.12-3.1.2.jar") \

#         self.schema = StructType([
#             StructField("symbol", StringType(), True),
#             StructField("type", StringType(), True),
#             StructField("exchange", StringType(), True),
#             StructField("market", StringType(), True),
#             StructField("price", DoubleType(), True),
#             StructField("size", IntegerType(), True),
#             StructField("bid", DoubleType(), True),
#             StructField("ask", DoubleType(), True),
#             StructField("volume", IntegerType(), True),
#             StructField("isContinuous", BooleanType(), True),
#             StructField("time", StringType(), True), 
#             StructField("serial", StringType(), True),
#             StructField("id", StringType(), True),
#             StructField("channel", StringType(), True)
#         ])
#         # self.latest_data = {}

#     def process_data(self):
#         kafka_df = self.spark.readStream \
#             .format("kafka") \
#             .option("kafka.bootstrap.servers", "kafka:9092") \
#             .option("subscribe", "raw_data") \
#             .option("startingOffsets", "latest") \
#             .load()
        
#         def process_batch(df, epoch_id):
#             if df.rdd.isEmpty():
#                 print(f"Batch {epoch_id} is empty, skipping processing.")
#                 return

#             df = df.selectExpr("CAST(value AS STRING) as json_data") \
#                 .select(from_json(col("json_data"), self.schema).alias("data")) \
#                 .select("data.*")
            
#             df = df.withColumn("time", to_timestamp(col("time") / 1000000))

#             df.printSchema()

# # 讀取出資料，並且建立計算基本資料，並且使用withWatermark然下面沒辦法用append
#             # .withWatermark("time", "1 minute")
#             windowed_df = df.groupBy(
#                 window(col("time"), "1 second"),
#                 col("symbol")
#             ).agg(
#                 spark_sum(col("price") * col("size")).alias("price_time_size"),  
#                 spark_sum("size").alias("size_per_sec"),
#                 last("volume", ignorenulls=True).alias("volume_till_now"),
#                 last("time", ignorenulls=True).alias("last_data_time"),
#                 last("isContinuous", ignorenulls=True).alias("isContinuous")
#             )
# # 計算每秒資料，並且調整欄位名稱，準備輸出
#             result_df = windowed_df.withColumn(
#                 "vwap_price_per_sec", col("price_time_size") / col("size_per_sec")
#             ).select(
#                 "symbol",
#                 "vwap_price_per_sec",
#                 "volume_till_now",
#                 "size_per_sec",
#                 "last_data_time",
#                 "isContinuous",
#                 "window.start", 
#                 "window.end",
#                 current_timestamp().alias("current_time") 
#                 # "last_serial as serial",  # 這個架構好像不支持這樣重新命名的操作
#                 # col("last_isClose").alias("isClose"),  # 不然就是要這樣命名
#             )

#             result_df.selectExpr(
#                 "CAST(symbol AS STRING) AS key",
#                 "to_json(struct(*)) AS value"
#             ).write \
#                 .format("kafka") \
#                 .option("kafka.bootstrap.servers", "kafka:9092") \
#                 .option("topic", "processed_data") \
#                 .save()
            
#         query = kafka_df.writeStream \
#             .foreachBatch(process_batch) \
#             .trigger(processingTime='1 second') \
#             .option("checkpointLocation", "/app/tmp/spark-checkpoints") \
#             .start()
#         query.awaitTermination()

#     def stop(self):
#         self.spark.stop()