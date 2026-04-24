const dbName = process.env.MONGO_DB || 'ride_ops_mg';
const dbRef = db.getSiblingDB(dbName);

// Create collections if not exists
if (!dbRef.getCollectionNames().includes('ride_events')) {
  dbRef.createCollection('ride_events');
}
if (!dbRef.getCollectionNames().includes('driver_location_stream')) {
  dbRef.createCollection('driver_location_stream');
}

// Indexes for analytics and CDC-friendly reads
dbRef.ride_events.createIndex({ ride_id: 1 });
dbRef.ride_events.createIndex({ event_time: 1 });
dbRef.ride_events.createIndex({ event_type: 1 });
dbRef.ride_events.createIndex({ driver_id: 1 });

dbRef.driver_location_stream.createIndex({ driver_id: 1 });
dbRef.driver_location_stream.createIndex({ ride_id: 1 });
dbRef.driver_location_stream.createIndex({ event_time: 1 });
dbRef.driver_location_stream.createIndex({ status_context: 1 });

print(`MongoDB initialized for database: ${dbName}`);
