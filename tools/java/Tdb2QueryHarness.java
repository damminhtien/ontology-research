import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

import org.apache.jena.query.Dataset;
import org.apache.jena.query.Query;
import org.apache.jena.query.QueryExecution;
import org.apache.jena.query.QueryFactory;
import org.apache.jena.query.ResultSetFactory;
import org.apache.jena.query.ResultSetFormatter;
import org.apache.jena.query.ResultSetRewindable;
import org.apache.jena.query.ReadWrite;
import org.apache.jena.tdb2.TDB2Factory;

/** Persistent in-process TDB2 query runner with engine-only timing. */
public final class Tdb2QueryHarness {
    private Tdb2QueryHarness() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException("usage: Tdb2QueryHarness <tdb2-directory>");
        }

        long openStarted = System.nanoTime();
        Dataset dataset = TDB2Factory.connectDataset(args[0]);
        long openNanos = System.nanoTime() - openStarted;
        System.out.println("READY\t" + openNanos);
        System.out.flush();

        try (BufferedReader input = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String encodedQuery;
            while ((encodedQuery = input.readLine()) != null) {
                String queryText = new String(
                        Base64.getDecoder().decode(encodedQuery), StandardCharsets.UTF_8);
                long queryStarted = System.nanoTime();
                Query query = QueryFactory.create(queryText);
                ResultSetRewindable results;
                dataset.begin(ReadWrite.READ);
                try {
                    try (QueryExecution execution = QueryExecution.dataset(dataset)
                            .query(query)
                            .build()) {
                        results = ResultSetFactory.copyResults(execution.execSelect());
                    }
                } finally {
                    dataset.end();
                }
                long queryNanos = System.nanoTime() - queryStarted;

                ByteArrayOutputStream output = new ByteArrayOutputStream();
                ResultSetFormatter.outputAsJSON(output, results);
                String encodedResult = Base64.getEncoder().encodeToString(
                        output.toByteArray());
                System.out.println(queryNanos + "\t" + encodedResult);
                System.out.flush();
            }
        } finally {
            dataset.close();
        }
    }
}
